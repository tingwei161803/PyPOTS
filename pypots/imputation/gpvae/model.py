"""
The implementation of GP-VAE for the partially-observed time-series imputation task.

Refer to the paper Fortuin V, Baranchuk D, Rätsch G, et al.
GP-VAE: Deep probabilistic time series imputation. AISTATS. PMLR, 2020: 1651-1661.

"""

# Created by Jun Wang <jwangfx@connect.ust.hk> and Wenjie Du <wenjay.du@gmail.com>
# License: GPL-v3


from typing import Union, Optional

import h5py
import numpy as np
import torch
import torch.nn as nn
from torch.utils.data import DataLoader

from .data import DatasetForGPVAE
from .modules import (
    Encoder,
    rbf_kernel,
    diffusion_kernel,
    matern_kernel,
    cauchy_kernel,
    Decoder,
)
from ..base import BaseNNImputer
from ...optim.adam import Adam
from ...optim.base import Optimizer


class _GPVAE(nn.Module):
    """model GPVAE with Gaussian Process prior

    Parameters
    ----------
    input_dim : int,
        the feature dimension of the input

    time_length : int,
        the length of each time series

    latent_dim : int,
        the feature dimension of the latent embedding

    encoder_sizes : tuple,
        the tuple of the network size in encoder

    decoder_sizes : tuple,
        the tuple of the network size in decoder

    beta : float,
        the weight of the KL divergence

    M : int,
        the number of Monte Carlo samples for ELBO estimation

    K : int,
        the number of importance weights for IWAE model

    kernel : str,
        the Gaussian Process kernel ["cauchy", "diffusion", "rbf", "matern"]

    sigma : float,
        the scale parameter for a kernel function

    length_scale : float,
        the length scale parameter for a kernel function

    kernel_scales : int,
        the number of different length scales over latent space dimensions
    """

    def __init__(
        self,
        input_dim,
        time_length,
        latent_dim,
        encoder_sizes=(64, 64),
        decoder_sizes=(64, 64),
        beta=1,
        M=1,
        K=1,
        kernel="cauchy",
        sigma=1.0,
        length_scale=7.0,
        kernel_scales=1,
        window_size=24,
    ):
        super().__init__()
        self.kernel = kernel
        self.sigma = sigma
        self.length_scale = length_scale
        self.kernel_scales = kernel_scales

        self.input_dim = input_dim
        self.time_length = time_length
        self.latent_dim = latent_dim
        self.beta = beta
        self.encoder = Encoder(input_dim, latent_dim, encoder_sizes, window_size)
        self.decoder = Decoder(latent_dim, input_dim, decoder_sizes)
        self.M = M
        self.K = K

        # Precomputed KL components for efficiency
        self.prior = self._init_prior()
        # self.pz_scale_inv = None
        # self.pz_scale_log_abs_determinant = None

    def encode(self, x):
        return self.encoder(x)

    def decode(self, z):
        if not torch.is_tensor(z):
            z = torch.tensor(z).float()
        num_dim = len(z.shape)
        assert num_dim > 2
        return self.decoder(torch.transpose(z, num_dim - 1, num_dim - 2))

    def forward(self, inputs, training=True):
        x = inputs["X"]
        m_mask = inputs["missing_mask"]
        x = x.repeat(self.M * self.K, 1, 1)
        if m_mask is not None:
            m_mask = m_mask.repeat(self.M * self.K, 1, 1)
            m_mask = m_mask.type(torch.bool)

        # pz = self.prior()
        qz_x = self.encode(x)
        z = qz_x.rsample()
        px_z = self.decode(z)

        nll = -px_z.log_prob(x)
        nll = torch.where(torch.isfinite(nll), nll, torch.zeros_like(nll))
        if m_mask is not None:
            nll = torch.where(m_mask, nll, torch.zeros_like(nll))
        nll = nll.sum(dim=(1, 2))

        if self.K > 1:
            kl = qz_x.log_prob(z) - self.prior.log_prob(z)
            kl = torch.where(torch.isfinite(kl), kl, torch.zeros_like(kl))
            kl = kl.sum(1)

            weights = -nll - kl
            weights = torch.reshape(weights, [self.M, self.K, -1])

            elbo = torch.logsumexp(weights, dim=1)
            elbo = elbo.mean()
        else:
            kl = self.kl_divergence(qz_x, self.prior)
            kl = torch.where(torch.isfinite(kl), kl, torch.zeros_like(kl))
            kl = kl.sum(1)

            elbo = -nll - self.beta * kl
            elbo = elbo.mean()

        imputed_data = self.decode(self.encode(x).mean).mean * ~m_mask + x * m_mask

        if not training:
            # if not in training mode, return the classification result only
            return {
                "imputed_data": imputed_data,
            }

        results = {
            "loss": -elbo.mean(),
            "imputed_data": imputed_data,
        }
        return results

    @staticmethod
    def kl_divergence(a, b):
        return torch.distributions.kl.kl_divergence(a, b)

    def _init_prior(self):
        # Compute kernel matrices for each latent dimension
        kernel_matrices = []
        for i in range(self.kernel_scales):
            if self.kernel == "rbf":
                kernel_matrices.append(
                    rbf_kernel(self.time_length, self.length_scale / 2**i)
                )
            elif self.kernel == "diffusion":
                kernel_matrices.append(
                    diffusion_kernel(self.time_length, self.length_scale / 2**i)
                )
            elif self.kernel == "matern":
                kernel_matrices.append(
                    matern_kernel(self.time_length, self.length_scale / 2**i)
                )
            elif self.kernel == "cauchy":
                kernel_matrices.append(
                    cauchy_kernel(
                        self.time_length, self.sigma, self.length_scale / 2**i
                    )
                )

        # Combine kernel matrices for each latent dimension
        tiled_matrices = []
        total = 0
        for i in range(self.kernel_scales):
            if i == self.kernel_scales - 1:
                multiplier = self.latent_dim - total
            else:
                multiplier = int(np.ceil(self.latent_dim / self.kernel_scales))
                total += multiplier
            tiled_matrices.append(
                torch.unsqueeze(kernel_matrices[i], 0).repeat(multiplier, 1, 1)
            )
        kernel_matrix_tiled = torch.cat(tiled_matrices)
        assert len(kernel_matrix_tiled) == self.latent_dim
        prior = torch.distributions.MultivariateNormal(
            loc=torch.zeros(self.latent_dim, self.time_length),
            covariance_matrix=kernel_matrix_tiled,
        )

        return prior


class GPVAE(BaseNNImputer):
    """The PyTorch implementation of the GPVAE model :cite:`fortuin2020GPVAEDeep`.

    Parameters
    ----------
    beta: float
        The weight of KL divergence in EBLO.

    kernel: str
        The type of kernel function chosen in the Gaussain Process Proir. ["cauchy", "diffusion", "rbf", "matern"]

    batch_size : int
        The batch size for training and evaluating the model.

    epochs : int
        The number of epochs for training the model.

    patience : int
        The patience for the early-stopping mechanism. Given a positive integer, the training process will be
        stopped when the model does not perform better after that number of epochs.
        Leaving it default as None will disable the early-stopping.

    optimizer : pypots.optim.base.Optimizer
        The optimizer for model training.
        If not given, will use a default Adam optimizer.

    num_workers : int
        The number of subprocesses to use for data loading.
        `0` means data loading will be in the main process, i.e. there won't be subprocesses.

    device : :class:`torch.device` or list
        The device for the model to run on. It can be a string, a :class:`torch.device` object, or a list of them.
        If not given, will try to use CUDA devices first (will use the default CUDA device if there are multiple),
        then CPUs, considering CUDA and CPU are so far the main devices for people to train ML models.
        If given a list of devices, e.g. ['cuda:0', 'cuda:1'], or [torch.device('cuda:0'), torch.device('cuda:1')] , the
        model will be parallely trained on the multiple devices (so far only support parallel training on CUDA devices).
        Other devices like Google TPU and Apple Silicon accelerator MPS may be added in the future.

    saving_path : str
        The path for automatically saving model checkpoints and tensorboard files (i.e. loss values recorded during
        training into a tensorboard file). Will not save if not given.

    model_saving_strategy : str
        The strategy to save model checkpoints. It has to be one of [None, "best", "better"].
        No model will be saved when it is set as None.
        The "best" strategy will only automatically save the best model after the training finished.
        The "better" strategy will automatically save the model during training whenever the model performs
        better than in previous epochs.

    References
    ----------
    .. [1] `Fortuin, V., Baranchuk, D., Raetsch, G. &amp; Mandt, S.. (2020).
        "GP-VAE: Deep Probabilistic Time Series Imputation".
        <i>Proceedings of the Twenty Third International Conference on Artificial Intelligence and Statistics</i>,
        in <i>Proceedings of Machine Learning Research</i> 108:1651-1661
        <https://proceedings.mlr.press/v108/fortuin20a.html>`_

    """

    def __init__(
        self,
        n_steps: int,
        n_features: int,
        latent_size: int,
        encoder_sizes: tuple = (64, 64),
        decoder_sizes: tuple = (64, 64),
        kernel: str = "cauchy",
        beta: float = 0.2,
        M: int = 1,
        K: int = 1,
        sigma: float = 1.0,
        length_scale: float = 7.0,
        kernel_scales: int = 1,
        window_size: int = 3,
        batch_size: int = 32,
        epochs: int = 100,
        patience: Optional[int] = None,
        optimizer: Optional[Optimizer] = Adam(),
        num_workers: int = 0,
        device: Optional[Union[str, torch.device, list]] = None,
        saving_path: str = None,
        model_saving_strategy: Optional[str] = "best",
    ):
        super().__init__(
            batch_size,
            epochs,
            patience,
            num_workers,
            device,
            saving_path,
            model_saving_strategy,
        )

        self.n_steps = n_steps
        self.n_features = n_features
        self.latent_size = latent_size
        self.kernel = kernel
        self.encoder_sizes = encoder_sizes
        self.decoder_sizes = decoder_sizes
        self.beta = beta
        self.M = M
        self.K = K
        self.sigma = sigma
        self.length_scale = length_scale
        self.kernel_scales = kernel_scales

        # set up the model
        self.model = _GPVAE(
            input_dim=self.n_features,
            time_length=self.n_steps,
            latent_dim=self.latent_size,
            kernel=self.kernel,
            encoder_sizes=self.encoder_sizes,
            decoder_sizes=self.decoder_sizes,
            beta=self.beta,
            M=self.M,
            K=self.K,
            sigma=self.sigma,
            length_scale=self.length_scale,
            kernel_scales=self.kernel_scales,
            window_size=window_size,
        )
        self._send_model_to_given_device()
        self._print_model_size()

        # set up the optimizer
        self.optimizer = optimizer
        self.optimizer.init_optimizer(self.model.parameters())

    def _assemble_input_for_training(self, data: list) -> dict:
        # fetch data
        (
            indices,
            X,
            missing_mask,
        ) = self._send_data_to_given_device(data)

        # assemble input data
        inputs = {
            "indices": indices,
            "X": X,
            "missing_mask": missing_mask,
        }

        return inputs

    def _assemble_input_for_validating(self, data: list) -> dict:
        return self._assemble_input_for_training(data)

    def _assemble_input_for_testing(self, data: list) -> dict:
        return self._assemble_input_for_validating(data)

    def fit(
        self,
        train_set: Union[dict, str],
        val_set: Optional[Union[dict, str]] = None,
        file_type: str = "h5py",
    ) -> None:
        # Step 1: wrap the input data with classes Dataset and DataLoader
        training_set = DatasetForGPVAE(
            train_set, return_labels=False, file_type=file_type
        )
        training_loader = DataLoader(
            training_set,
            batch_size=self.batch_size,
            shuffle=True,
            num_workers=self.num_workers,
        )
        val_loader = None
        if val_set is not None:
            if isinstance(val_set, str):
                with h5py.File(val_set, "r") as hf:
                    # Here we read the whole validation set from the file to mask a portion for validation.
                    # In PyPOTS, using a file usually because the data is too big. However, the validation set is
                    # generally shouldn't be too large. For example, we have 1 billion samples for model training.
                    # We won't take 20% of them as the validation set because we want as much as possible data for the
                    # training stage to enhance the model's generalization ability. Therefore, 100,000 representative
                    # samples will be enough to validate the model.
                    val_set = {
                        "X": hf["X"][:],
                        "X_intact": hf["X_intact"][:],
                        "indicating_mask": hf["indicating_mask"][:],
                    }
            val_set = DatasetForGPVAE(val_set, return_labels=False, file_type=file_type)
            val_loader = DataLoader(
                val_set,
                batch_size=self.batch_size,
                shuffle=False,
                num_workers=self.num_workers,
            )

        # Step 2: train the model and freeze it
        self._train_model(training_loader, val_loader)
        self.model.load_state_dict(self.best_model_dict)
        self.model.eval()  # set the model as eval status to freeze it.

        # Step 3: save the model if necessary
        self._auto_save_model_if_necessary(training_finished=True)

    def impute(
        self,
        X: Union[dict, str],
        file_type="h5py",
    ) -> np.ndarray:
        self.model.eval()  # set the model as eval status to freeze it.
        test_set = DatasetForGPVAE(X, return_labels=False, file_type=file_type)
        test_loader = DataLoader(
            test_set,
            batch_size=self.batch_size,
            shuffle=False,
            num_workers=self.num_workers,
        )
        imputation_collector = []

        with torch.no_grad():
            for idx, data in enumerate(test_loader):
                inputs = self._assemble_input_for_testing(data)
                results = self.model.forward(inputs, training=False)
                imputed_data = results["imputed_data"]
                imputation_collector.append(imputed_data)

        imputation_collector = torch.cat(imputation_collector)
        return imputation_collector.cpu().detach().numpy()