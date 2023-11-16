"""ADMET-AI class to contain ADMET model and prediction function."""
from collections import defaultdict
from multiprocessing import Pool
from pathlib import Path

import numpy as np
import pandas as pd
import torch
from chemfunc.molecular_fingerprints import compute_rdkit_fingerprint
from chemprop.data import (
    MoleculeDataLoader,
    MoleculeDatapoint,
    MoleculeDataset,
    set_cache_graph,
    set_cache_mol,
)
from chemprop.data.data import SMILES_TO_MOL
from chemprop.models import MoleculeModel
from chemprop.train import predict
from chemprop.utils import load_args, load_checkpoint, load_scalers
from rdkit import Chem
from scipy.stats import percentileofscore
from sklearn.preprocessing import StandardScaler
from tqdm import tqdm


class ADMETModel:
    """ADMET-AI model class."""

    # TODO: set defaults for model paths in constants and include model files in git repo; same for DrugBank
    def __init__(
        self,
        model_dirs: list[Path | str],
        drugbank_reference_path: Path | None = None,
        atc_code: str | None = None,
        num_workers: int = 8,
        cache_molecules: bool = True,
        fingerprint_multiprocessing_min: int = 100,
    ) -> None:
        """Initialize the ADMET-AI model.

        :param model_dirs: List of paths to directories, where each directory contains
                           an ensemble of Chemprop-RDKit models.
        :param drugbank_reference_path: Path to a CSV file containing DrugBank approved molecules
                                        with ADMET predictions and ATC codes.
        :param atc_code: The ATC code to filter the DrugBank reference set by.
                         If None, the entire DrugBank reference set will be used.
        :param num_workers: Number of workers for the data loader.
        :param cache_molecules: Whether to cache molecules. Caching improves prediction speed but requires more memory.
        :param fingerprint_multiprocessing_min: Minimum number of molecules for multiprocessing to be used for
                                                fingerprint computation. Otherwise, single processing is used.
        """
        # Check parameters
        if atc_code is not None and drugbank_reference_path is None:
            raise ValueError(
                "DrugBank reference path must be provided if ATC code is provided."
            )

        # Save parameters
        self.num_workers = num_workers
        self.cache_molecules = cache_molecules
        self.fingerprint_multiprocessing_min = fingerprint_multiprocessing_min
        self._atc_code = atc_code

        # Load DrugBank reference set if needed
        if drugbank_reference_path is not None:
            # Load DrugBank DataFrame
            self.drugbank = pd.read_csv(drugbank_reference_path)

            # Map ATC codes to all indices of the drugbank with that ATC code
            atc_code_to_drugbank_indices = defaultdict(set)
            for atc_column in [
                column for column in self.drugbank.columns if column.startswith("atc_")
            ]:
                for index, atc_codes in self.drugbank[atc_column].dropna().items():
                    for atc_code in atc_codes.split(";"):
                        atc_code_to_drugbank_indices[atc_code.lower()].add(index)

            # Save ATC code to indices mapping to global variable and convert set to sorted list
            self.atc_code_to_drugbank_indices = {
                atc_code: sorted(indices)
                for atc_code, indices in atc_code_to_drugbank_indices.items()
            }
        else:
            self.drugbank = None

        # Set ATC code (setting the code also filters the DrugBank by ATC code)
        self.atc_code = self._atc_code

        # Set caching
        set_cache_graph(self.cache_molecules)
        set_cache_mol(self.cache_molecules)

        # Set device based on GPU availability
        self.device = (
            torch.device("cuda") if torch.cuda.is_available() else torch.device("cpu")
        )

        # Load each ensemble of models
        self.task_lists: list[list[str]] = []
        self.use_features_list: list[bool] = []
        self.model_lists: list[list[MoleculeModel]] = []
        self.scaler_lists: list[list[StandardScaler | None]] = []

        for model_dir in model_dirs:
            # Get model paths for the ensemble in the directory
            model_paths = sorted(Path(model_dir).glob("**/*.pt"))

            # Load args for this ensemble
            train_args = load_args(str(model_paths[0]))

            # Get task names for this ensemble
            task_names = train_args.task_names
            self.task_lists.append(task_names)

            # Get whether to use features for this ensemble
            use_features = train_args.use_input_features
            self.use_features_list.append(use_features)

            # Load models in the ensemble
            models = [
                load_checkpoint(path=str(model_path), device=self.device).eval()
                for model_path in model_paths
            ]
            self.model_lists.append(models)

            # Load scalers for each model
            scalers = [
                load_scalers(path=str(model_path))[0] for model_path in model_paths
            ]
            self.scaler_lists.append(scalers)

        # Ensure all models do or do not use features
        if not len(set(self.use_features_list)) == 1:
            raise ValueError("All models must either use or not use features.")

        self.use_features = self.use_features_list[0]

    @property
    def num_ensembles(self) -> int:
        """Get the number of ensembles."""
        return len(self.model_lists)

    @property
    def atc_code(self) -> str | None:
        """Get the ATC code."""
        return self._atc_code

    @atc_code.setter
    def atc_code(self, atc_code: str | None) -> None:
        """Set the ATC code and filter DrugBank based on provided ATC code.

        :param atc_code: The ATC code to filter the DrugBank reference set by.
                         If None, the entire DrugBank reference set will be used.
        """
        # Handle case of no DrugBank
        if self.drugbank is None:
            raise ValueError(
                "Cannot set ATC code if DrugBank reference is not provided."
            )

        # Validate ATC code
        if atc_code is not None and atc_code not in self.atc_code_to_drugbank_indices:
            raise ValueError(f"Invalid ATC code: {atc_code}")

        # Save ATC code
        self._atc_code = atc_code

        # Filter DrugBank by ATC code if needed
        if self.atc_code is not None:
            self.drugbank_atc_filtered = self.drugbank.loc[
                self.atc_code_to_drugbank_indices[self.atc_code]
            ]
        else:
            self.drugbank_atc_filtered = self.drugbank

    def predict(self, smiles: str | list[str]) -> pd.DataFrame:
        """Make predictions on a list of SMILES strings.

        :param smiles: A SMILES string or a list of SMILES strings.
        :return: A DataFrame containing the predictions with SMILES strings as the index.
        """
        # Convert SMILES to list if needed
        if isinstance(smiles, str):
            smiles = [smiles]

        # Convert SMILES to RDKit molecules and cache if desired
        mols = []
        for smile in tqdm(smiles, desc="SMILES to Mol"):
            if smile in SMILES_TO_MOL:
                mol = SMILES_TO_MOL[smile]
            else:
                mol = Chem.MolFromSmiles(smile)

            mols.append(mol)

            if self.cache_molecules:
                SMILES_TO_MOL[smile] = mol

        # Remove invalid molecules
        invalid_mols = [mol is None for mol in mols]

        if any(invalid_mols):
            print(f"Warning: {sum(invalid_mols):,} invalid molecules will be removed")

            mols = [mol for mol in mols if mol is not None]
            smiles = [
                smile for smile, invalid in zip(smiles, invalid_mols) if not invalid
            ]

        # Compute fingerprints if needed
        if self.use_features:
            # Select between multiprocessing and single processing
            if len(mols) >= self.fingerprint_multiprocessing_min:
                pool = Pool()
                map_fn = pool.imap
            else:
                pool = None
                map_fn = map

            # Compute fingerprints
            fingerprints = np.array(
                list(
                    tqdm(
                        map_fn(compute_rdkit_fingerprint, mols),
                        total=len(mols),
                        desc=f"RDKit fingerprints",
                    )
                )
            )

            # Close pool if needed
            if pool is not None:
                pool.close()
        else:
            fingerprints = [None] * len(smiles)

        # Build data loader
        data_loader = MoleculeDataLoader(
            dataset=MoleculeDataset(
                [
                    MoleculeDatapoint(smiles=[smile], features=fingerprint,)
                    for smile, fingerprint in zip(smiles, fingerprints)
                ]
            ),
            num_workers=self.num_workers,
            shuffle=False,
        )

        # Make predictions
        task_to_preds = {}

        # Loop through each ensemble and make predictions
        for tasks, use_features, models, scalers in tqdm(
            zip(
                self.task_lists,
                self.use_features_list,
                self.model_lists,
                self.scaler_lists,
            ),
            total=self.num_ensembles,
            desc="model ensembles",
        ):
            # Make predictions
            preds = [
                predict(model=model, data_loader=data_loader)
                for model in tqdm(models, desc="individual models")
            ]

            # Scale predictions if needed (for regression)
            if scalers[0] is not None:
                preds = [
                    scaler.inverse_transform(pred).astype(float)
                    for scaler, pred in zip(scalers, preds)
                ]

            # Average ensemble predictions
            preds = np.mean(preds, axis=0)

            # Add predictions to data
            for i, task in enumerate(tasks):
                task_to_preds[task] = preds[:, i]

        # Put preds in a DataFrame
        preds = pd.DataFrame(task_to_preds, index=smiles)

        # Compute DrugBank percentiles if needed
        if self.drugbank is not None:
            # Set DrugBank suffix
            if self.atc_code is None:
                drugbank_suffix = "drugbank_approved_percentile"
            else:
                drugbank_suffix = f"drugbank_approved_{self.atc_code}_percentile"

            # Compute DrugBank percentiles
            drugbank_percentiles = pd.DataFrame(
                data={
                    f"{property_name}_{drugbank_suffix}": percentileofscore(
                        self.drugbank_atc_filtered[property_name],
                        preds[property_name].values,
                    )
                    for property_name in preds.columns
                },
                index=smiles,
            )

            # Combine predictions and percentiles
            preds = pd.concat((preds, drugbank_percentiles), axis=1)

        return preds
