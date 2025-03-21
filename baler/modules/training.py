# Copyright 2022 Baler Contributors

# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at

#     http://www.apache.org/licenses/LICENSE-2.0

# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import os
import random
import time
import sys

import numpy as np
import torch
from torch.utils.data import DataLoader
from tqdm import tqdm

from ..modules import diagnostics
from ..modules import helper
from ..modules import utils
from torch.nn import functional as F


def fit(
    config,
    model,
    train_dl,
    model_children,
    regular_param,
    optimizer,
    latent_dim,
    RHO,
    l1,
    n_dimensions,
):
    """This function trains the model on the train set. It computes the losses and does the backwards propagation, and updates the optimizer as well.
    Args:
        model (modelObject): The model you wish to train
        train_dl (torch.DataLoader): Defines the batched data which the model is trained on
        model_children (list): List of model parameters
        regular_param (float): Determines proportionality constant for the gradient descent step.
        optimizer (torch.optim): Chooses optimizer for gradient descent.
        RHO (float): Float used for KL Divergence (Not currently a feature)
        l1 (boolean): If `True`, use L1 regularization. Otherwise, don't.
        n_dimensions (int): Number of dimensions.
    Returns:
        list, model object: Training loss and trained model
    """

    print("### Beginning Training")

    model.train()

    running_loss = 0.0
    device = helper.get_device()

    for idx, inputs in enumerate(tqdm(train_dl)):
        inputs = inputs.to(device)

        # Set the gradients to zero
        optimizer.zero_grad()

        # Compute the predicted outputs from the input data
        reconstructions = model(inputs)

        # print('reconstructions shape: ', reconstructions.shape)
        # print('max value from reconstructions: ', np.amax(reconstructions.detach().numpy()))
        # print('min value from reconstructions: ', np.amin(reconstructions.detach().numpy()))

        if (
            hasattr(config, "custom_loss_function")
            and config.custom_loss_function == "loss_function_swae"
        ):
            z = model.encode(inputs)
            loss, mse_loss, l1_loss = utils.loss_function_swae(
                inputs, z, reconstructions, latent_dim
            )
        else:
            # Compute how far off the prediction is
            loss, mse_loss, l1_loss = utils.mse_sum_loss_l1(
                model_children=model_children,
                true_data=inputs,
                reconstructed_data=reconstructions,
                reg_param=regular_param,
                validate=True,
            )

        # Compute the loss-gradient with
        loss.backward()

        # Update the optimizer
        optimizer.step()

        running_loss += loss.item()

    epoch_loss = running_loss / (idx + 1)
    print(f"# Finished. Training Loss: {loss:.6f}")
    return epoch_loss, mse_loss, l1_loss, model


def validate(model, test_dl, model_children, reg_param):
    """Function used to validate the training. Not necessary for doing compression, but gives a good indication of wether the model selected is a good fit or not.
    Args:
        model (modelObject): Defines the model one wants to validate. The model used here is passed directly from `fit()`.
        test_dl (torch.DataLoader): Defines the batched data which the model is validated on
        model_children (list): List of model parameters
        regular_param (float): Determines proportionality constant for the gradient descent step.
    Returns:
        float: Validation loss
    """
    print("### Beginning Validating")

    model.eval()

    running_loss = 0.0
    device = helper.get_device()

    with torch.no_grad():
        for idx, inputs in enumerate(tqdm(test_dl)):
            inputs = inputs.to(device)
            reconstructions = model(inputs)

            loss, _, _ = utils.mse_sum_loss_l1(
                model_children=model_children,
                true_data=inputs,
                reconstructed_data=reconstructions,
                reg_param=reg_param,
                validate=True,
            )
            running_loss += loss.item()

    epoch_loss = running_loss / (idx + 1)
    print(f"# Finished. Validation Loss: {loss:.6f}")
    return epoch_loss


def seed_worker(worker_id):
    """PyTorch implementation to fix the seeds
    Args:
        worker_id ():
    """
    worker_seed = torch.initial_seed() % 2**32
    np.random.seed(worker_seed)
    random.seed(worker_seed)


def train(model, variables, train_data, test_data, project_path, config):
    """Does the entire training loop by calling the `fit()` and `validate()`. Appart from this, this is the main function where the data is converted
        to the correct type for it to be trained, via `torch.Tensor()`. Furthermore, the batching is also done here, based on `config.batch_size`,
        and it is the `torch.utils.data.DataLoader` doing the splitting.
        Applying either `EarlyStopping` or `LR Scheduler` is also done here, all based on their respective `config` arguments.
        For reproducibility, the seeds can also be fixed in this function.
    Args:
        model (modelObject): The model you wish to train
        variables (_type_): _description_
        train_set (ndarray): Array consisting of the train set
        test_set (ndarray): Array consisting of the test set
        project_path (string): Path to the project directory
        config (dataClass): Base class selecting user inputs
    Returns:
        modelObject: fully trained model ready to perform compression and decompression
    """
    # Fix the random seed - TODO: add flag to make this optional

    if config.deterministic_algorithm:
        random.seed(0)
        torch.manual_seed(0)
        np.random.seed(0)
        torch.use_deterministic_algorithms(True)
        g = torch.Generator()
        g.manual_seed(0)

    test_size = config.test_size
    learning_rate = config.lr
    bs = config.batch_size
    reg_param = config.reg_param
    rho = config.RHO
    l1 = config.l1
    epochs = config.epochs
    latent_space_size = config.latent_space_size
    intermittent_model_saving = config.intermittent_model_saving
    intermittent_saving_patience = config.intermittent_saving_patience

    model_children = list(model.children())

    # Initialize model with appropriate device
    device = helper.get_device()
    model = model.to(device)

    # Converting data to tensors
    if config.data_dimension == 2:
        if config.model_type == "dense":
            # print(train_data.shape)
            # print(test_data.shape)
            # sys.exit()
            train_ds = torch.tensor(
                train_data, dtype=torch.float32, device=device
            ).view(train_data.shape[0], train_data.shape[1] * train_data.shape[2])
            valid_ds = torch.tensor(test_data, dtype=torch.float32, device=device).view(
                test_data.shape[0], test_data.shape[1] * test_data.shape[2]
            )
        elif config.model_type == "convolutional" and config.model_name == "Conv_AE_3D":
            train_ds = torch.tensor(
                train_data, dtype=torch.float32, device=device
            ).view(
                train_data.shape[0] // bs,
                1,
                bs,
                train_data.shape[1],
                train_data.shape[2],
            )
            valid_ds = torch.tensor(test_data, dtype=torch.float32, device=device).view(
                train_data.shape[0] // bs,
                1,
                bs,
                train_data.shape[1],
                train_data.shape[2],
            )
        elif config.model_type == "convolutional":
            train_ds = torch.tensor(
                train_data, dtype=torch.float32, device=device
            ).view(train_data.shape[0], 1, train_data.shape[1], train_data.shape[2])
            valid_ds = torch.tensor(test_data, dtype=torch.float32, device=device).view(
                train_data.shape[0], 1, train_data.shape[1], train_data.shape[2]
            )
    elif config.data_dimension == 1:
        train_ds = torch.tensor(train_data, dtype=torch.float64, device=device)
        valid_ds = torch.tensor(test_data, dtype=torch.float64, device=device)

    # Pushing input data into the torch-DataLoader object and combines into one DataLoader object (a basic wrapper
    # around several DataLoader objects).

    if config.deterministic_algorithm:
        train_dl = DataLoader(
            train_ds,
            batch_size=bs,
            shuffle=False,
            worker_init_fn=seed_worker,
            generator=g,
            drop_last=False,
        )
        valid_dl = DataLoader(
            valid_ds,
            batch_size=bs,
            worker_init_fn=seed_worker,
            generator=g,
            drop_last=False,
        )
    else:
        train_dl = DataLoader(
            train_ds,
            batch_size=bs,
            shuffle=False,
            drop_last=False,
        )
        valid_dl = DataLoader(
            valid_ds,
            batch_size=bs,
            drop_last=False,
        )

    # Select Optimizer
    optimizer = torch.optim.Adam(model.parameters(), lr=learning_rate)

    # Activate early stopping
    if config.early_stopping:
        early_stopping = utils.EarlyStopping(
            patience=config.early_stopping_patience, min_delta=config.min_delta
        )  # Changes to patience & min_delta can be made in configs

    # Activate LR Scheduler
    if config.lr_scheduler:
        lr_scheduler = utils.LRScheduler(
            optimizer=optimizer, patience=config.lr_scheduler_patience
        )

    # Training and Validation of the model
    train_loss = []
    val_loss = []
    start = time.time()

    # Registering hooks for activation extraction
    if config.activation_extraction:
        hooks = model.store_hooks()

    for epoch in range(epochs):
        print(f"Epoch {epoch + 1} of {epochs}")

        train_epoch_loss, mse_loss_fit, regularizer_loss_fit, trained_model = fit(
            config=config,
            model=model,
            train_dl=train_dl,
            model_children=model_children,
            regular_param=reg_param,
            optimizer=optimizer,
            latent_dim=latent_space_size,
            RHO=rho,
            l1=l1,
            n_dimensions=config.data_dimension,
        )
        train_loss.append(train_epoch_loss)

        if test_size:
            val_epoch_loss = validate(
                model=trained_model,
                test_dl=valid_dl,
                model_children=model_children,
                reg_param=reg_param,
            )
            val_loss.append(val_epoch_loss)
        else:
            val_epoch_loss = train_epoch_loss
            val_loss.append(val_epoch_loss)

        if config.lr_scheduler:
            lr_scheduler(val_epoch_loss)
        if config.early_stopping:
            early_stopping(val_epoch_loss)
            if early_stopping.early_stop:
                break

        ## Implementation to save models & values after every N epochs, where N is stored in 'intermittent_saving_patience':
        if intermittent_model_saving:
            if epoch % intermittent_saving_patience == 0:
                path = os.path.join(project_path, f"model_{epoch}.pt")
                helper.model_saver(model, path)

    end = time.time()

    # Saving activations values
    if config.activation_extraction:
        activations = diagnostics.dict_to_square_matrix(model.get_activations())
        model.detach_hooks(hooks)
        np.save(os.path.join(project_path, "activations.npy"), activations)

    print(f"{(end - start) / 60:.3} minutes")
    np.save(
        os.path.join(project_path, "loss_data.npy"), np.array([train_loss, val_loss])
    )

    if config.model_type == "convolutional":
        final_layer = model.get_final_layer_dims()
        np.save(os.path.join(project_path, "final_layer.npy"), np.array(final_layer))

    return trained_model
