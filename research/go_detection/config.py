import os
from dataclasses import dataclass, field
from typing import List, Optional

from go_detection.common.asset_io import AssetIO


# Fully qaualified class name can be obtained by: <class>.__module__ + "." + <class>.__qualname__
@dataclass
class DataCfg:
    base_path: str
    train_split_percent: float = 0.8
    randomize_train_split: bool = True
    use_dynamic_dataset: bool = True

    _target_: str = f"{__module__}.{__qualname__}"

    def get_asset_io(self) -> AssetIO:
        return AssetIO(self.base_path)


@dataclass
class ResultCfg:
    name: str
    dir: str

    _target_: str = f"{__module__}.{__qualname__}"

    def get_asset_io(self) -> AssetIO:
        return AssetIO(os.path.join(self.dir, self.name))


@dataclass
class ModelCfg:
    _target_: str = f"{__module__}.{__qualname__}"


@dataclass
class SimCfg:
    data_cfg: DataCfg
    model_cfg: ModelCfg
    result_cfg: ResultCfg

    _target_: str = f"{__module__}.{__qualname__}"
