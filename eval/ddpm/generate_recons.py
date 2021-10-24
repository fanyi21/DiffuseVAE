import copy

import hydra
import pytorch_lightning as pl
import torch
from datasets.latent import ZipDataset
from datasets.recons import ReconstructionDataset
from models.callbacks import ImageWriter
from models.diffusion import DDPM, DDPMWrapper, SuperResModel
from models.vae import VAE
from pytorch_lightning.utilities.seed import seed_everything
from torch.utils.data import DataLoader, TensorDataset
from util import configure_device


def __parse_str(s):
    split = s.split(",")
    return [int(s) for s in split if s != "" and s is not None]


@hydra.main(config_path="configs")
def generate_recons(config):
    config_ddpm = config.dataset.ddpm
    config_vae = config.dataset.vae
    seed_everything(config_ddpm.evaluation.seed, workers=True)

    batch_size = config_ddpm.evaluation.batch_size
    n_steps = config_ddpm.evaluation.n_steps
    n_samples = config_ddpm.evaluation.n_samples
    image_size = config_ddpm.data.image_size

    # Load pretrained VAE
    vae = VAE.load_from_checkpoint(
        config_vae.evaluation.chkpt_path,
        input_res=image_size,
        enc_block_str=config_vae.model.enc_block_config,
        dec_block_str=config_vae.model.dec_block_config,
        enc_channel_str=config_vae.model.enc_channel_config,
        dec_channel_str=config_vae.model.dec_channel_config,
    )
    vae.eval()

    # Load pretrained wrapper
    attn_resolutions = __parse_str(config_ddpm.model.attn_resolutions)
    dim_mults = __parse_str(config_ddpm.model.dim_mults)
    decoder = SuperResModel(
        in_channels=config_ddpm.data.n_channels,
        model_channels=config_ddpm.model.dim,
        out_channels=3,
        num_res_blocks=config_ddpm.model.n_residual,
        attention_resolutions=attn_resolutions,
        channel_mult=dim_mults,
        use_checkpoint=False,
        dropout=config_ddpm.model.dropout,
        num_heads=config_ddpm.model.n_heads,
    )

    ema_decoder = copy.deepcopy(decoder)
    decoder.eval()
    ema_decoder.eval()

    online_ddpm = DDPM(
        decoder,
        beta_1=config_ddpm.model.beta1,
        beta_2=config_ddpm.model.beta2,
        T=config_ddpm.model.n_timesteps,
    )
    target_ddpm = DDPM(
        ema_decoder,
        beta_1=config_ddpm.model.beta1,
        beta_2=config_ddpm.model.beta2,
        T=config_ddpm.model.n_timesteps,
    )

    # NOTE: Using strict=False since the VAE model is not included
    # in the pretrained DDPM state_dict
    ddpm_wrapper = DDPMWrapper.load_from_checkpoint(
        config_ddpm.evaluation.chkpt_path,
        online_network=online_ddpm,
        target_network=target_ddpm,
        vae=vae,
        conditional=True,
        strict=False,
        pred_steps=n_steps,
        eval_mode="recons",
    )

    # Create predict dataset of reconstructions
    recons_dataset = ReconstructionDataset(
        config_ddpm.data.root, norm=config.data.norm, subsample_size=n_samples
    )
    ddpm_latent_dataset = TensorDataset(
        torch.randn(n_samples, 3, image_size, image_size)
    )
    pred_dataset = ZipDataset(recons_dataset, ddpm_latent_dataset)

    # Setup devices
    test_kwargs = {}
    loader_kws = {}
    device = config_ddpm.evaluation.device
    if device.startswith("gpu"):
        _, devs = configure_device(device)
        test_kwargs["gpus"] = devs

        # Disable find_unused_parameters when using DDP training for performance reasons
        loader_kws["persistent_workers"] = True
    elif device == "tpu":
        test_kwargs["tpu_cores"] = 8

    # Predict loader
    val_loader = DataLoader(
        pred_dataset,
        batch_size=batch_size,
        drop_last=False,
        pin_memory=True,
        shuffle=False,
        num_workers=config_ddpm.evaluation.workers,
        **loader_kws,
    )

    # Predict trainer
    write_callback = ImageWriter(
        config_ddpm.evaluation.save_path,
        "batch",
        n_steps=n_steps,
        eval_mode="recons",
        conditional=True,
        sample_prefix=config_ddpm.evaluation.sample_prefix,
        save_mode=config_ddpm.evaluation.save_mode,
        save_vae=config_ddpm.evaluation.save_vae,
    )

    test_kwargs["callbacks"] = [write_callback]
    test_kwargs["default_root_dir"] = config_ddpm.evaluation.save_path
    trainer = pl.Trainer(**test_kwargs)
    trainer.predict(ddpm_wrapper, val_loader)


if __name__ == "__main__":
    generate_recons()
