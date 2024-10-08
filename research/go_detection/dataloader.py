import itertools
import logging
import random
import sys
from collections import defaultdict
from dataclasses import dataclass
from typing import List, cast

import numpy as np
import torch
import torchvision.transforms as transforms
from go_detection.common.asset_io import AssetIO
from go_detection.config import DataCfg
from matplotlib import pyplot as plt
from PIL import Image
from torch.utils.data import DataLoader, Dataset, Sampler
from torchvision.transforms.functional import crop
from tqdm import tqdm


@dataclass
class DataPointPath:
    image_path: str
    label_path: str
    board_path: str


@dataclass
class DataPoint:
    image: torch.Tensor
    label: torch.Tensor
    board_pt: torch.Tensor

    def cpu(self):
        return DataPoint(
            self.image.cpu(),
            self.label.cpu(),
            self.board_pt.cpu(),
        )

    def cuda(self):
        return DataPoint(
            self.image.cuda(),
            self.label.cuda(),
            self.board_pt.cuda(),
        )

    def to_data_points(self) -> "DataPoints":
        return DataPoints(
            self.image.unsqueeze(0),
            self.label.unsqueeze(0),
            self.board_pt.unsqueeze(0),
        )


@dataclass
class DataPoints:
    images: torch.Tensor
    labels: torch.Tensor
    board_pts: torch.Tensor

    def cpu(self):
        return DataPoints(
            self.images.cpu(),
            self.labels.cpu(),
            self.board_pts.cpu(),
        )

    def cuda(self):
        return DataPoints(
            self.images.cuda(),
            self.labels.cuda(),
            self.board_pts.cuda(),
        )

    def get_point(self, index) -> DataPoint:
        assert index < self.images.shape[0]
        return DataPoint(
            self.images[index],
            self.labels[index],
            self.board_pts[index],
        )


logger = logging.getLogger(__name__)


class InfiniteSampler(Sampler):
    def __init__(self, length: int, shuffle: bool, repeat: bool):
        assert length > 0
        self.length = length
        self.shuffle = shuffle
        self.repeat = repeat

    def __len__(self):
        return self.length

    def __iter__(self):
        order = list(range(self.length))
        while True:
            if self.shuffle:
                random.shuffle(order)

            for idx in order:
                yield idx

            if not self.repeat:
                return


def custom_collate_fn(batches) -> DataPoints:
    images = []
    labels = []
    board_pts = []

    for batch in batches:
        batch = cast(DataPoint, batch)
        images.append(batch.image)
        labels.append(batch.label)
        board_pts.append(batch.board_pt)

        assert images[0].shape == batch.image.shape
        assert labels[0].shape == batch.label.shape
        assert board_pts[0].shape == batch.board_pt.shape

    images = torch.stack(images, dim=0)
    labels = torch.stack(labels, dim=0)
    board_pts = torch.stack(board_pts, dim=0)
    return DataPoints(images, labels, board_pts)


def _read_image(data_io: AssetIO, image_path: str, board_metadata):
    """
    Returns tuple:
        - resized image
        - original (width x height) before resizing
    """
    orig_image = data_io.load_image(image_path)
    _, height, width = orig_image.shape
    original_size = torch.tensor([width, height])
    new_size = (1024, 1024)  # Specify the new size (height, width)

    if width == height:
        resize_transform = transforms.Resize(new_size)
        resized_tensor = resize_transform(orig_image)
        return resized_tensor, original_size

    assert "pts_clicks" in board_metadata
    board_pts = torch.tensor(board_metadata.pts_clicks).float()

    center_square = board_pts.mean(dim=0)
    # square_half_length will perfectly keep one dimension. If width is larger, then square_half_length will be half_height of image.
    square_half_length = torch.tensor(min(width, height) / 2).float().ceil()
    if width > height:
        center_square[1] = height / 2
    else:
        assert width < height
        center_square[0] = width / 2

    all_points_are_inside = all(
        [
            ((board_pts - (center_square - square_half_length)) >= 0.0).all(),
            ((board_pts - (center_square + square_half_length)) <= 0.0).all(),
        ]
    )

    if not all_points_are_inside:
        # increase square_half_length a small amount until the board perfectly fits in

        required_half_length = (board_pts - center_square).abs().max().ceil()
        assert (
            min(width, height) < 2 * required_half_length < max(width, height)
        ), f"The required length cannot be bigger than both width and height. We expect it to only be bigger than one of them"

        # Check that all the points are inside this bigger region
        assert all(
            [
                ((board_pts - (center_square - required_half_length)) >= 0.0).all(),
                ((board_pts - (center_square + required_half_length)) <= 0.0).all(),
            ]
        ), "Expected all the board points to be inside the square after increasing square_half_length"

        load_mode = 1
        if load_mode == 0:
            # In this mode, we do not want aspect ratio to change at all. This mode starts with the best fit square, and expands it in BOTH dimension till it fits all the points. This will cause some black padding to appear. Then we can further expand it using the extra_expand parameter. A higher value will cause more black padding, but will also increase the amount of background infomation in the image
            extra_expand = 60
            rectangle_half_length = torch.tensor(
                [required_half_length, required_half_length]
            )
            rectangle_half_length = rectangle_half_length + extra_expand

        elif load_mode == 1:
            # In this mode, we do not want any black padding at all. This mode starts with the best fit square, and expands it in ONE dimension till it fits all the points. Then we can furthur expand it using extra_expand parameter. A higher value will increase the amount of background info in the image. Once we crop the rectangular region, it gets resized to a square, so this mode does not preserve the aspect ratio
            extra_expand = 60
            rectangle_half_length = (
                [required_half_length + extra_expand, square_half_length]
                if width > height
                else [square_half_length, required_half_length + extra_expand]
            )
            rectangle_half_length = torch.tensor(rectangle_half_length)

        else:
            assert False, f"Unknown load mode: {load_mode}"
    else:
        # only need to crop
        rectangle_half_length = torch.tensor([square_half_length, square_half_length])

    # Check that all the points fit inside the crop region
    all(
        [
            ((board_pts - (center_square - rectangle_half_length)) >= 0.0).all(),
            ((board_pts - (center_square + rectangle_half_length)) <= 0.0).all(),
        ]
    ), "Expected all the board points to be inside the rectangular crop region"
    intermediate_image = crop(
        orig_image,
        int(center_square[1] - rectangle_half_length[1]),
        int(center_square[0] - rectangle_half_length[0]),
        int(2 * rectangle_half_length[1]),
        int(2 * rectangle_half_length[0]),
    )

    resize_transform = transforms.Resize(new_size)
    resized_image = resize_transform(intermediate_image)

    # asset_io = AssetIO("/home/rmenon/Desktop/dev/projects/aigo/research")
    # asset_io.save_image("rishi_orig.png", orig_image)
    # asset_io.save_image("rishi_intermediate.png", intermediate_image)
    # asset_io.save_image("rishi_final.png", resized_image)

    return resized_image, original_size


def _read_label(data_io: AssetIO, label_path: str):
    """
    Returns a tensor where index:
        Black: 0
        Empty: 1
        White: 2
    Returns
        A tensor of Boardsize x Boardsize x 3
    """
    with open(data_io.get_abs(label_path), "r") as file:
        lines = file.read()
        label = []
        for line in lines.split("\n"):
            if line == "":
                continue

            label_line = []
            for ch in line.split(" "):
                if ch.upper() == "B":
                    digit = 0
                elif ch == ".":
                    digit = 1
                elif ch.upper() == "W":
                    digit = 2
                else:
                    assert (
                        False
                    ), f"Unknown character: '{ch}' in line '{line}', file: {label_path}"
                label_line.append(digit)
            label.append(label_line)

        label = torch.tensor(label)
        return label


def _load_single(data_point: DataPointPath, data_io: AssetIO) -> DataPoint:
    board_metadata = data_io.load_yaml(data_point.board_path)

    label = _read_label(data_io, data_point.label_path)
    image, original_size = _read_image(data_io, data_point.image_path, board_metadata)
    image = image[:3, :, :]  # Remove the alpha channel
    # board_pt is a list of 4 points. The first point is the top left corner, and then the points are in clockwise order
    board_pt = torch.tensor(board_metadata.pts_clicks) / original_size

    return DataPoint(image, label, board_pt)


def _load(entire_data: List[DataPointPath], data_io: AssetIO, include_logs=True):
    images = []
    labels = []
    board_pts = []

    if include_logs:
        iters = tqdm(entire_data, desc="Loading dataset")
    else:
        iters = iter(entire_data)

    for data_point in iters:
        image, label, board_pt = _load_single(data_point)
        images.append(image)
        labels.append(label)
        board_pts.append(board_pt)

    return images, labels, board_pts


class GoDataset(Dataset):
    def __init__(
        self,
        datapoint_paths: List[DataPointPath],
        base_path: str,
    ):
        self.datapoint_paths = datapoint_paths
        self.images, self.labels, self.board_pts = _load(
            datapoint_paths, AssetIO(base_path)
        )

    def __len__(self):
        return len(self.labels)

    def __getitem__(self, idx) -> DataPoint:
        data_point = DataPoint(self.images[idx], self.labels[idx], self.board_pts[idx])
        return data_point.cuda()


class GoDynamicDataset(Dataset):
    """
    We cannot load the entire dataset into memory so load it dynamically
    """

    def __init__(
        self,
        datapoint_paths: List[DataPointPath],
        base_path: str,
    ):
        self.datapoint_paths = datapoint_paths
        self.data_io = AssetIO(base_path)

    def __len__(self):
        return len(self.datapoint_paths)

    def __getitem__(self, idx) -> DataPoint:
        data_point = _load_single(self.datapoint_paths[idx], self.data_io)
        return data_point.cuda()


def load_datasets(
    cfg: DataCfg,
    train_datapoint_paths: List[DataPointPath],
    test_datapoint_paths: List[DataPointPath],
):
    if cfg.use_dynamic_dataset:
        train_dataset = GoDynamicDataset(train_datapoint_paths, cfg.base_path)
        test_dataset = GoDynamicDataset(test_datapoint_paths, cfg.base_path)
    else:
        train_dataset = GoDataset(train_datapoint_paths, cfg.base_path)
        test_dataset = GoDataset(test_datapoint_paths, cfg.base_path)

    train_dataloader = DataLoader(
        train_dataset,
        batch_size=cfg.train_batch_size,
        # shuffle=True,
        sampler=InfiniteSampler(len(train_dataset), True, False),
        collate_fn=custom_collate_fn,
        # num_workers=4,
        # multiprocessing_context="spawn",
    )

    test_dataloader = DataLoader(
        test_dataset,
        batch_size=cfg.test_batch_size,
        sampler=InfiniteSampler(len(test_dataset), False, False),
        collate_fn=custom_collate_fn,
        # num_workers=4,
        # multiprocessing_context="spawn",
    )
    return train_dataloader, test_dataloader


def create_datasets(cfg: DataCfg):
    data_io = AssetIO(cfg.base_path)
    directories = sorted(data_io.ls())

    entire_data: List[List[DataPointPath]] = []
    for directory in directories:
        if not data_io.has_dir(directory):
            continue

        files = data_io.ls(directory, only_file_name=False)
        map_name_to_dict = defaultdict(dict)
        board_file = None

        for file_name in files:
            file_split = file_name.rsplit(".", 1)

            key = None
            if len(file_split) == 2 and file_split[-1].lower() in ["png", "jpg"]:
                key = "image"
            elif len(file_split) == 2 and file_split[-1].lower() in ["txt"]:
                key = "label"
            elif "board_extractor_state" in file_split[0]:
                board_file = file_name

            if key is None:
                continue

            map_name_to_dict[file_split[0]][key] = file_name

        directory_data: List[DataPointPath] = []
        for val in map_name_to_dict.values():
            if "label" in val and "image" in val:
                data = DataPointPath(
                    val["image"],
                    val["label"],
                    board_file,
                )
                directory_data.append(data)

        if directory_data:
            entire_data.append(directory_data)

    if cfg.randomize_train_split:
        random.shuffle(entire_data)

    # Calculate the train test split
    directory_counts = [len(_) for _ in entire_data]
    directory_cumsum = np.cumsum(directory_counts)
    directory_cumsum = directory_cumsum / directory_cumsum[-1]

    # Split the train and test dataset based on the directorys. This is done because one directory has the same board and background. So we don't want images to spill from train dataset to test dataset
    split_index = np.searchsorted(directory_cumsum, cfg.train_split_percent, "right")
    train, test = entire_data[:split_index], entire_data[split_index:]
    assert len(train) + len(test) == len(entire_data)

    # If you want validation dataset as well
    # split_index = np.searchsorted(directory_cumsum, [0.7, 0.9], "right")
    # train, validate, test = (
    #     entire_data[: split_index[0]],
    #     entire_data[split_index[0] : split_index[1]],
    #     entire_data[split_index[1] :],
    # )
    # assert len(train) + len(validate) + len(test) == len(entire_data)

    train = list(itertools.chain(*train))
    test = list(itertools.chain(*test))

    return load_datasets(cfg, train, test)
