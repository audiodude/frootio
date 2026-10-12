# Vendored from https://github.com/kwatcharasupat/bandit-v2 (src/models/base.py),
# Apache-2.0. Modified for frootio: derives from torch.nn.Module instead of
# pytorch_lightning.LightningModule. See NOTICE.
from torch import nn


class BaseEndToEndModule(nn.Module):
    def __init__(
        self,
    ) -> None:
        super().__init__()
