import lightning as L
from lightning.pytorch.utilities import grad_norm

import torch
from torch import nn
import torch.nn.functional as F

import numpy as np
import auraloss
import math

import cached_conv as cc

from modules.filterbank import FilterBank


from typing import List, Tuple, Optional

class NoiseBandNet(L.LightningModule):
  """
  A neural network that learns how to resynthesise signal, predicting amplitudes of
  precalculated, loopable noise bands.

  Args:
    - m_filters: int, the number of filters in the filterbank
    - hidden_size: int, the size of the hidden layers of the neural network
    - hidden_layers: int, the number of hidden layers of the neural network
    - n_control_params: int, the number of control parameters to be used
    - samplerate : int, the sampling rate of the input signal
    - resampling_factor: int, internal up / down sampling factor for control signal and noisebands
    - learning_rate: float, the learning rate for the optimizer
    - torch_device: str, the device to run the model on
  """
  def __init__(self,
               m_filters: int = 2048,
               samplerate: int = 44100,
               hidden_size: int = 128,
               hidden_layers: int = 3,
               n_control_params: int = 2,
               resampling_factor: int = 32,
               learning_rate: float = 1e-3,
               torch_device = 'cpu'):
    super().__init__()
    # Save hyperparameters in the checkpoints
    self.save_hyperparameters()

    self._filterbank = FilterBank(
      m_filters=m_filters,
      fs=samplerate
    )
    self.resampling_factor = resampling_factor
    self.n_control_params = n_control_params

    self._hidden_layers = hidden_layers
    self._torch_device = torch_device
    self._samplerate = samplerate
    self._noisebands_shift = 0

    # Define the neural network
    ## Parallel connection of the control parameters to the dedicated MLPs
    self.control_param_mlps = nn.ModuleList([self._make_mlp(1, 1, hidden_size) for _ in range(n_control_params)])

    ## Intermediate GRU layer
    self.gru = nn.GRU((hidden_size) * n_control_params, hidden_size, batch_first=True)

    ## Intermediary 3-layer MLP
    self.inter_mlp = self._make_mlp(hidden_size + n_control_params, self._hidden_layers, hidden_size)

    ## Output layer predicting amplitudes
    self.output_amps = nn.Linear(hidden_size, len(self._noisebands))

    # Define the loss
    self.loss = self._construct_loss_function()

    self._learning_rate = learning_rate


  def forward(self, control_params: List[torch.Tensor], init_hidden_state: Optional[torch.Tensor] = None) -> Tuple[torch.Tensor, torch.Tensor]:
    """
    Forward pass of the network.
    Args:
      - control_params: List[torch.Tensor[batch_size, signal_length, 1]], a list of control parameters
      - init_hidden_state: torch.Tensor[1, batch_size, hidden_size], the initial hidden state of the GRU
    Returns:
      - signal: torch.Tensor, the synthesized signal
    """
    # predict the amplitudes of the noise bands
    amps, hidden_state = self._predict_amplitudes(control_params, init_hidden_state)

    # synthesize the signal
    signal = self._synthesize(amps)
    return signal, hidden_state


  def training_step(self, batch: torch.Tensor, batch_idx: int) -> torch.Tensor:
    """
    Compute the loss for a batch of data

    Args:
      batch:
        Tuple[
            torch.Tensor[batch_size, n_signal],
            torch.Tensor[params_number, batch_size, n_signal]
          ], audio, control_params
      batch_idx: int, index of the batch (unused)
    Returns:
      loss: torch.Tensor[batch_size, 1], tensor of loss
    """
    x_audio, control_params = batch

    # Downsample the control params by resampling factor
    control_params = [F.interpolate(c, scale_factor=1/self.resampling_factor, mode='linear') for c in control_params]

    # Predict the audio
    y_audio, _ = self.forward(control_params)

    # Compute return the loss
    loss = self.loss(y_audio, x_audio)
    self.log("train_loss", loss, prog_bar=True, logger=True)
    return loss

  # TODO: Generate the validationa audio and add to tensorboard
  # def on_validation_epoch_end(self, )


  def configure_optimizers(self):
    return torch.optim.Adam(self.parameters(), lr=self._learning_rate)


  def _predict_amplitudes(self,
                          control_params: List[torch.Tensor],
                          init_hidden_state: Optional[torch.Tensor] = None) -> Tuple[torch.Tensor, torch.Tensor]:
    """
    Predict noiseband amplitudes given the control parameters.
    Args:
      - control_params: List[torch.Tensor[batch_size, 1, signal_length]], a list of control parameters
      - init_hidden_state: torch.Tensor[1, batch_size, hidden_size], the initial hidden state of the GRU
    Returns:
      - amps, hidden_state: Tuple[torch.Tensor, torch.Tensor], the predicted amplitudes of the noise bands and the last hidden state of the GRU
    """
    control_params = [c.permute(0, 2, 1) for c in control_params]

    # pass through the control parameter MLPs
    # x = [mlp(param) for param, mlp in zip(tuple(control_params), self.control_param_mlps)] # out: [control_params_number, batch_size, signal_length, hidden_size]
    x = []
    for i, mlp in enumerate(self.control_param_mlps):
      x.append(mlp(control_params[i]))
    # out: [control_params_number, batch_size, signal_length, hidden_size]

    # concatenate both mlp outputs together
    x = torch.cat(x, dim=-1) # out: [batch_size, signal_length, hidden_size * control_params_number]

    # pass concatenated control parameter outputs through GRU
    # GRU returns (output, final_hidden_state) tuple. We are interested in the output.
    # and in the hidden state only in case of streaming application
    x, hidden_state = self.gru(x, hx=init_hidden_state) # out: [batch_size, signal_length, hidden_size]
    # x = self.gru(x)[0] # out: [batch_size, signal_length, hidden_size]

    # append the control params to the GRU output
    for c in control_params:
      x = torch.cat([x, c], dim=-1) # out: [batch_size, signal_length, hidden_size + control_params_number]

    # pass through the intermediary MLP
    x = self.inter_mlp(x) # out: (batch_size, signal_length, hidden_size)

    # pass through the output layer and custom activation
    amps = self._scaled_sigmoid(self.output_amps(x)).permute(0, 2, 1) # out: [batch_size, n_bands, signal_length]
    return amps, hidden_state


  def _synthesize(self, amplitudes: torch.Tensor) -> torch.Tensor:
    """
    Synthesizes a signal from the predicted amplitudes and the baked noise bands.
    Args:
      - amplitudes: torch.Tensor[batch_size, n_bands, sig_length], the predicted amplitudes of the noise bands
    Returns:
      - signal: torch.Tensor[batch_size, sig_length], the synthesized signal
    """
    # upsample the amplitudes
    upsampled_amplitudes = F.interpolate(amplitudes, scale_factor=float(self.resampling_factor), mode='linear')

    # shift the noisebands to maintain the continuity of the noise signal
    noisebands = torch.roll(self._noisebands, shifts=-self._noisebands_shift, dims=-1)

    if self.training:
      # roll the noisebands randomly to avoid overfitting to the noise values
      # check whether model is training
      noisebands = torch.roll(noisebands, shifts=int(torch.randint(0, noisebands.shape[-1], size=(1,))), dims=-1)

    # fit the noisebands into the mplitudes
    repeats = math.ceil(upsampled_amplitudes.shape[-1] / noisebands.shape[-1])
    looped_bands = noisebands.repeat(1, repeats) # repeat
    looped_bands = looped_bands[:, :upsampled_amplitudes.shape[-1]] # trim
    looped_bands = looped_bands.to(upsampled_amplitudes.device, dtype=torch.float32)

    # Save the noisebands shift for the next iteration
    self._noisebands_shift = (self._noisebands_shift + upsampled_amplitudes.shape[-1]) % self._noisebands.shape[-1]

    # synthesize the signal
    signal = torch.sum(upsampled_amplitudes * looped_bands, dim=1, keepdim=True)
    return signal


  def _scaled_sigmoid(self, x: torch.Tensor):
    """
    Custom activation function for the output layer. It is a scaled sigmoid function,
    guaranteeing that the output is always positive.
    Args:
      - x: torch.Tensor, the input tensor
    Returns:
      - y: torch.Tensor, the output tensor
    """
    return 2*torch.pow(torch.sigmoid(x), math.log(10)) + 1e-18


  @property
  def _noisebands(self):
    """Delegate the noisebands to the filterbank object."""
    return self._filterbank.noisebands


  @torch.jit.ignore
  def _construct_loss_function(self):
    """
    Construct the loss function for the model: a multi-resolution STFT loss
    """
    fft_sizes = np.array([8192, 4096, 2048, 1024, 512, 128, 32])
    return auraloss.freq.MultiResolutionSTFTLoss(fft_sizes=[8192, 4096, 2048, 1024, 512, 128, 32],
                                                hop_sizes=fft_sizes//4,
                                                win_lengths=fft_sizes)


  @staticmethod
  def _make_mlp(in_size: int, hidden_layers: int, hidden_size: int) -> cc.CachedSequential:
    """
    Constructs a multi-layer perceptron.
    Args:
    - in_size: int, the input layer size
    - hidden_layers: int, the number of hidden layers
    - hidden_size: int, the size of each hidden layer
    Returns:
    - mlp: cc.CachedSequential, the multi-layer perceptron
    """
    sizes = [in_size]
    sizes.extend(hidden_layers * [hidden_size])

    layers = []
    for i in range(len(sizes)-1):
      layers.append(nn.Linear(sizes[i], sizes[i+1]))
      layers.append(nn.LayerNorm(sizes[i+1]))
      layers.append(nn.LeakyReLU())

    return cc.CachedSequential(*layers)
