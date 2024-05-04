import lightning as L
from lightning.pytorch.loggers import TensorBoardLogger
from lightning.pytorch.callbacks import EarlyStopping, ModelCheckpoint

import torch
torch.set_default_dtype(torch.float32)

import argparse
import os

from torch.utils.data import DataLoader
from audio_dataset import AudioDataset

from modules import NoiseBandNet
from modules.callbacks import BetaWarmupCallback, CyclicalBetaWarmupCallback

if __name__ == '__main__':
  parser = argparse.ArgumentParser()
  parser.add_argument('--dataset_path', help='Directory of the training sound/sounds')
  parser.add_argument('--device', help='Device to use', default='cuda', choices=['cuda', 'cpu'])
  parser.add_argument('--batch_size', type=int, default=16, help='Batch size for training')
  parser.add_argument('--lr', type=float, default=1e-4, help='Learning rate')
  parser.add_argument('--n_band', type=int, default=512, help='Number of bands of the filter bank')
  parser.add_argument('--fs', type=int, default=44100, help='Sampling rate of the audio')
  parser.add_argument('--encoder_ratios', type=int, nargs='+', default=[8, 4, 2], help='Capacity ratios for the encoder')
  parser.add_argument('--decoder_ratios', type=int, nargs='+', default=[2, 4, 8], help='Capacity ratios for the decoder')
  parser.add_argument('--capacity', type=int, default=64, help='Capacity of the model')
  parser.add_argument('--latent_size', type=int, default=16, help='Dimensionality of the latent space')
  parser.add_argument('--audio_chunk_duration', type=float, default=1.5, help='Duration of the audio chunks in seconds')
  parser.add_argument('--resampling_factor', type=int, default=32, help='Resampling factor for the control signal and noise bands')
  parser.add_argument('--mixed_precision', type=bool, default=False, help='Use mixed precision')
  parser.add_argument('--training_dir', type=str, default='training', help='Directory to save the training logs')
  parser.add_argument('--model_name', type=str, default='noisebandnet', help='Name of the model')
  parser.add_argument('--max_epochs', type=int, default=10000, help='Maximum number of epochs')
  parser.add_argument('--control_params', type=str, nargs='+', default=['loudness', 'centroid'], help='Control parameters to use, possible: aloudness, centroid, flatness')
  parser.add_argument('--beta', type=float, default=1.0, help='Beta parameter for the beta-VAE loss')
  parser.add_argument('--warmup_start', type=int, default=300, help='Step to start the beta warmup')
  parser.add_argument('--warmup_end', type=int, default=1300, help='Step to end the beta warmup')
  parser.add_argument('--kld_weight', type=float, default=0.001, help='Weight for the KLD loss')
  parser.add_argument('--early_stopping', type=bool, default=False, help='Use early stopping')
  # parser.add_argument('--warmup_cycle', type=int, default=50, help='Number of epochs for a full beta cycle')
  config = parser.parse_args()

  n_signal = int(config.audio_chunk_duration * config.fs)

  os.makedirs(os.path.join(config.training_dir, config.model_name), exist_ok=True)

  dataset = AudioDataset(
    dataset_path=config.dataset_path,
    n_signal=n_signal,
    sampling_rate=config.fs,
  )

  train_loader = DataLoader(dataset, batch_size=config.batch_size, shuffle=True)

  nbn = NoiseBandNet(
    latent_size=config.latent_size,
    encoder_ratios=config.encoder_ratios,
    decoder_ratios=config.decoder_ratios,
    capacity=config.capacity,
    learning_rate=config.lr,
    samplerate=config.fs,
    m_filters=config.n_band,
    resampling_factor=config.resampling_factor,
    torch_device=config.device,
    kld_weight=config.kld_weight,
  )

  tb_logger = TensorBoardLogger(config.training_dir, name=config.model_name)

  # Beta parameter warmup
  # beta_warmup = BetaWarmupCallback(
  #   beta=config.beta,
  #   start_epoch=config.warmup_start,
  #   end_epoch=config.warmup_end
  # )

  # Warming up beta parameter
  beta_warmup = BetaWarmupCallback(
    beta=config.beta,
    start_steps=config.warmup_start,
    end_steps=config.warmup_end
  )

  training_callbacks = [beta_warmup]

  # Early stopping
  if config.early_stopping:
    training_callbacks += [EarlyStopping(monitor='train_loss', patience=10, mode='min')]

  # Define the checkpoint callback
  checkpoint_callback = ModelCheckpoint(
      filename='best',
      monitor='train_loss',
      mode='min',
  )
  training_callbacks += [checkpoint_callback]

  precision = 16 if config.mixed_precision else 32
  trainer = L.Trainer(
    callbacks=training_callbacks,
    max_epochs=config.max_epochs,
    accelerator=config.device,
    precision=precision,
    log_every_n_steps=4,
    logger=tb_logger
  )
  trainer.fit(model=nbn, train_dataloaders=train_loader)
