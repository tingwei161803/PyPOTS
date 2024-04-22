"""
The implementation of GRU-D for the partially-observed time-series imputation task.

Refer to the paper "Che, Z., Purushotham, S., Cho, K., Sontag, D.A., & Liu, Y. (2018).
Recurrent Neural Networks for Multivariate Time Series with Missing Values. Scientific Reports."

"""

# Created by Wenjie Du <wenjay.du@gmail.com>
# License: BSD-3-Clause


from typing import Union

import torch
import torch.nn as nn
import torch.nn.functional as F

# from ....nn.modules.rnn import TemporalDecay
from ...nn.modules.grud import BackboneGRUD


class _GRUD(nn.Module):
    def __init__(
        self,
        n_steps: int,
        n_features: int,
        rnn_hidden_size: int,
        n_classes: int,
        device: Union[str, torch.device],
    ):
        super().__init__()
        self.n_steps = n_steps
        self.n_features = n_features
        self.rnn_hidden_size = rnn_hidden_size
        self.n_classes = n_classes
        self.device = device

        # create models
        self.model = BackboneGRUD(
            n_steps,
            n_features,
            rnn_hidden_size,
        )
        self.classifier = nn.Linear(self.rnn_hidden_size, self.n_classes)

    def forward(self, inputs: dict, training: bool = True, output_hidden = False) -> dict:
        """Forward processing of GRU-D.

        Parameters
        ----------
        inputs :
            The input data.

        training :
            Whether in training mode.

        Returns
        -------
        dict,
            A dictionary includes all results.
        """
        X = inputs["X"]
        missing_mask = inputs["missing_mask"]
        deltas = inputs["deltas"]
        empirical_mean = inputs["empirical_mean"]
        X_filledLOCF = inputs["X_filledLOCF"]

        _, hidden_state = self.model(
            X, missing_mask, deltas, empirical_mean, X_filledLOCF
        )
        # print("Hidden State")
        # print(hidden_state)
        # print(type(hidden_state))

        logits = self.classifier(hidden_state)
        classification_pred = torch.softmax(logits, dim=1)
        results = {"classification_pred": classification_pred}

        # if in training mode, return results with losses
        if training:
            torch.log(classification_pred)
            classification_loss = F.nll_loss(
                torch.log(classification_pred), inputs["label"]
            )
            results["loss"] = classification_loss
        
        if output_hidden:
            return results, hidden_state
        else:
            return results
