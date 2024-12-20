# MIT License
#
# Copyright (c) 2022 NVIDIA CORPORATION & AFFILIATES, University of Washington. All rights reserved.
#
# Permission is hereby granted, free of charge, to any person obtaining a
# copy of this software and associated documentation files (the "Software"),
# to deal in the Software without restriction, including without limitation
# the rights to use, copy, modify, merge, publish, distribute, sublicense,
# and/or sell copies of the Software, and to permit persons to whom the
# Software is furnished to do so, subject to the following conditions:
#
# The above copyright notice and this permission notice shall be included in
# all copies or substantial portions of the Software.
#
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
# IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
# FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT.  IN NO EVENT SHALL
# THE AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
# LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING
# FROM, OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER
# DEALINGS IN THE SOFTWARE.

from pathlib import Path
from typing import Dict, Optional, Union

import lightning.pytorch as pl
import numpy as np
import torch
from robofin.collision import FrankaCollisionSpheres
from robofin.kinematics.numba import franka_arm_link_fk
from robofin.robot_constants import RealFrankaConstants
from robofin.samplers import NumpyFrankaSampler
from torch.utils.data import DataLoader, Dataset

from avoid_everything.dataset import Dataset as MPNDataset
from avoid_everything.geometry import construct_mixed_point_cloud
from avoid_everything.normalization import normalize_franka_joints
from avoid_everything.type_defs import DatasetType


class Base(Dataset):
    """
    This base class should never be used directly, but it handles the filesystem
    management and the basic indexing. When using these dataloaders, the directory
    holding the data should look like so:
        directory/
          train/
             train.hdf5
          val/
             val.hdf5
          test/
             test.hdf5
    Note that only the relevant subdirectory is required, i.e. when creating a
    dataset for training, this class will not check for (and will not use) the val/
    and test/ subdirectories.
    """

    def __init__(
        self,
        data_path: Union[Path, str],
        dataset_type: DatasetType,
        trajectory_key: str,
        num_robot_points: int,
        num_obstacle_points: int,
        num_target_points: int,
        prismatic_joint: float,
        random_scale: float,
    ):
        """
        :param directory Path: The path to the root of the data directory
        :param num_robot_points int: The number of points to sample from the robot
        :param num_obstacle_points int: The number of points to sample from the obstacles
        :param num_target_points int: The number of points to sample from the target
                                      robot end effector
        :param dataset_type DatasetType: What type of dataset this is
        :param random_scale float: The standard deviation of the random normal
                                   noise to apply to the joints during training.
                                   This is only used for train datasets.
        """
        self._database = Path(data_path)
        self.trajectory_key = trajectory_key
        self.train = dataset_type == DatasetType.TRAIN
        self.normalization_params = None
        if not self.file_exists:
            self.state_count = 0
            self.problem_count = 0
        else:
            with MPNDataset(self._database) as f:
                self.state_count = len(f[self.trajectory_key])
                self.problem_count = len(f)

        self.num_obstacle_points = num_obstacle_points
        self.num_robot_points = num_robot_points
        self.num_target_points = num_target_points
        self.prismatic_joint = prismatic_joint
        self.random_scale = random_scale
        self.franka_sampler = NumpyFrankaSampler(
            num_robot_points=self.num_robot_points,
            num_eef_points=self.num_target_points,
            use_cache=True,
            with_base_link=True,
        )

        self.cooo = FrankaCollisionSpheres()

    @property
    def file_exists(self) -> bool:
        return self._database.exists()

    @property
    def md5_checksum(self):
        with MPNDataset(self._database) as f:
            return f.md5_checksum

    @classmethod
    def load_from_directory(
        cls,
        directory: Union[Path, str],
        dataset_type: DatasetType,
        *args,
        **kwargs,
    ):
        directory = Path(directory)
        if dataset_type in (DatasetType.TRAIN, "train"):
            enclosing_path = directory / "train"
            data_path = enclosing_path / "train.hdf5"
        elif dataset_type in (DatasetType.VAL_STATE, "val"):
            enclosing_path = directory / "val"
            data_path = enclosing_path / "val.hdf5"
        elif dataset_type in (DatasetType.VAL, "val"):
            enclosing_path = directory / "val"
            data_path = enclosing_path / "val.hdf5"
        elif dataset_type in (DatasetType.MINI_TRAIN, "mini_train"):
            enclosing_path = directory / "val"
            data_path = enclosing_path / "mini_train.hdf5"
        elif dataset_type in (DatasetType.VAL_PRETRAIN, "val_pretrain"):
            enclosing_path = directory / "val"
            data_path = enclosing_path / "val_pretrain.hdf5"
        elif dataset_type in (DatasetType.TEST, "test"):
            enclosing_path = directory / "test"
            data_path = enclosing_path / "test.hdf5"
        else:
            raise Exception(f"Invalid dataset type: {dataset_type}")
        return cls(
            data_path,
            dataset_type,
            *args,
            **kwargs,
        )

    @classmethod
    def clamp_and_normalize(cls, configuration_tensor: torch.Tensor):
        """
        Normalizes the joints between -1 and 1 according the the joint limits

        :param configuration_tensor torch.Tensor: The input tensor. Has dim [7]
        """
        limits = torch.as_tensor(RealFrankaConstants.JOINT_LIMITS).float()
        configuration_tensor = torch.minimum(
            torch.maximum(configuration_tensor, limits[:, 0]), limits[:, 1]
        )
        return normalize_franka_joints(configuration_tensor)

    def get_inputs(self, problem, flobs) -> Dict[str, torch.Tensor]:
        """
        Loads all the relevant data and puts it in a dictionary. This includes
        normalizing all configurations and constructing the pointcloud.
        If a training dataset, applies some randomness to joints (before
        sampling the pointcloud).

        :param trajectory_idx int: The index of the trajectory in the hdf5 file
        :param timestep int: The timestep within that trajectory
        :rtype Dict[str, torch.Tensor]: The data used aggregated by the dataloader
                                        and used for training
        """
        item = {}
        target_pose = franka_arm_link_fk(
            problem.target, self.prismatic_joint, np.eye(4)
        )[RealFrankaConstants.ARM_LINKS.right_gripper]
        target_points = torch.as_tensor(
            self.franka_sampler.sample_end_effector(
                target_pose,
                self.prismatic_joint,
            )[..., :3]
        ).float()
        item["target_position"] = torch.as_tensor(target_pose[:3, 3]).float()
        item["target_orientation"] = torch.as_tensor(target_pose[:3, :3]).float()

        item["cuboid_dims"] = torch.as_tensor(flobs.cuboid_dims).float()
        item["cuboid_centers"] = torch.as_tensor(flobs.cuboid_centers).float()
        item["cuboid_quats"] = torch.as_tensor(flobs.cuboid_quaternions).float()

        item["cylinder_radii"] = torch.as_tensor(flobs.cylinder_radii).float()
        item["cylinder_heights"] = torch.as_tensor(flobs.cylinder_heights).float()
        item["cylinder_centers"] = torch.as_tensor(flobs.cylinder_centers).float()
        item["cylinder_quats"] = torch.as_tensor(flobs.cylinder_quaternions).float()

        scene_points = torch.as_tensor(
            construct_mixed_point_cloud(problem.obstacles, self.num_obstacle_points)[
                ..., :3
            ]
        ).float()
        item["point_cloud"] = torch.cat((scene_points, target_points), dim=0)
        item["point_cloud_labels"] = torch.cat(
            (
                torch.ones(len(scene_points), 1),
                2 * torch.ones(len(target_points), 1),
            )
        )

        return item


class TrajectoryDataset(Base):
    """
    This dataset is used exclusively for validating. Each element in the dataset
    represents a trajectory start and scene. There is no supervision because
    this is used to produce an entire rollout and check for success. When doing
    validation, we care more about success than we care about matching the
    expert's behavior (which is a key difference from training).
    """

    def __init__(
        self,
        data_path: Union[Path, str],
        dataset_type: DatasetType,
        trajectory_key: str,
        num_robot_points: int,
        num_obstacle_points: int,
        num_target_points: int,
        prismatic_joint: float,
        random_scale: float = 0.0,
    ):
        """
        :param directory Path: The path to the root of the data directory
        :param num_robot_points int: The number of points to sample from the robot
        :param num_obstacle_points int: The number of points to sample from the obstacles
        :param num_target_points int: The number of points to sample from the target
                                      robot end effector
        :param dataset_type DatasetType: What type of dataset this is
        """
        super().__init__(
            data_path,
            dataset_type,
            trajectory_key,
            num_robot_points,
            num_obstacle_points,
            num_target_points,
            prismatic_joint,
            random_scale,
        )

    @classmethod
    def load_from_directory(
        cls,
        directory: Path,
        trajectory_key: str,
        num_robot_points: int,
        num_obstacle_points: int,
        num_target_points: int,
        dataset_type: DatasetType,
        prismatic_joint: float,
        random_scale: float,
    ):
        return super().load_from_directory(
            directory,
            dataset_type,
            trajectory_key,
            num_robot_points,
            num_obstacle_points,
            num_target_points,
            prismatic_joint,
            random_scale,
        )

    def __len__(self):
        """
        Necessary for Pytorch. For this dataset, the length is the total number
        of problems
        """
        return self.problem_count

    def unpadded_expert(self, pidx: int):
        with MPNDataset(self._database, "r") as f:
            return torch.as_tensor(f[self.trajectory_key].expert(pidx))

    def __getitem__(self, pidx: int) -> Dict[str, torch.Tensor]:
        """
        Required by Pytorch. Queries for data at a particular index. Note that
        in this dataset, the index always corresponds to the trajectory index.

        :param pidx int: The problem index
        :rtype Dict[str, torch.Tensor]: Returns a dictionary that can be assembled
            by the data loader before using in training.
        """
        with MPNDataset(self._database, "r") as f:
            problem = f[self.trajectory_key].problem(pidx)
            flobs = f[self.trajectory_key].flattened_obstacles(pidx)
            item = self.get_inputs(problem, flobs)
            config = f[self.trajectory_key].problem(pidx).q0
            config_tensor = torch.as_tensor(config).float()

            if self.train:
                # Add slight random noise to the joints
                randomized = (
                    self.random_scale * torch.randn(config_tensor.shape) + config_tensor
                )

                item["configuration"] = self.clamp_and_normalize(randomized)
                robot_points = self.franka_sampler.sample(
                    randomized.numpy(), self.prismatic_joint
                )[:, :3]
            else:
                item["configuration"] = self.clamp_and_normalize(config_tensor)
                robot_points = self.franka_sampler.sample(
                    config_tensor.numpy(),
                    self.prismatic_joint,
                )[:, :3]
            robot_points = torch.as_tensor(robot_points).float()

            item["point_cloud"] = torch.cat((robot_points, item["point_cloud"]), dim=0)
            item["point_cloud_labels"] = torch.cat(
                (
                    torch.zeros(len(robot_points), 1),
                    item["point_cloud_labels"],
                )
            )
            item["expert"] = torch.as_tensor(f[self.trajectory_key].padded_expert(pidx))
        item["pidx"] = torch.as_tensor(pidx)

        return item


class StateDataset(Base):
    """
    This is the dataset used primarily for training. Each element in the dataset
    represents the robot and scene at a particular time $t$. Likewise, the
    supervision is the robot's configuration at q_{t+1}.
    """

    def __init__(
        self,
        data_path: Union[Path, str],
        dataset_type: DatasetType,
        trajectory_key: str,
        num_robot_points: int,
        num_obstacle_points: int,
        num_target_points: int,
        prismatic_joint: float,
        random_scale: float,
        action_chunk_length: int,
    ):
        """
        :param directory Path: The path to the root of the data directory
        :param num_robot_points int: The number of points to sample from the robot
        :param num_obstacle_points int: The number of points to sample from the obstacles
        :param num_target_points int: The number of points to sample from the target
                                      robot end effector
        :param dataset_type DatasetType: What type of dataset this is
        :param random_scale float: The standard deviation of the random normal
                                   noise to apply to the joints during training.
                                   This is only used for train datasets.
        """
        super().__init__(
            data_path,
            dataset_type,
            trajectory_key,
            num_robot_points,
            num_obstacle_points,
            num_target_points,
            prismatic_joint,
            random_scale,
        )
        self.action_chunk_length = action_chunk_length

    @classmethod
    def load_from_directory(
        cls,
        directory: Path,
        trajectory_key: str,
        num_robot_points: int,
        num_obstacle_points: int,
        num_target_points: int,
        dataset_type: DatasetType,
        prismatic_joint: float,
        random_scale: float,
        action_chunk_length: int,
    ):
        return super().load_from_directory(
            directory,
            dataset_type,
            trajectory_key,
            num_robot_points,
            num_obstacle_points,
            num_target_points,
            prismatic_joint,
            random_scale,
            action_chunk_length=action_chunk_length,
        )

    def __len__(self):
        """
        Returns the total number of start configurations in the dataset (i.e.
        the length of the trajectories times the number of trajectories)

        """
        return self.state_count

    def __getitem__(self, idx: int) -> Dict[str, torch.Tensor]:
        """
        Returns a training datapoint representing a single configuration in a
        single scene with the configuration at the next timestep as supervision

        :param idx int: Index represents the timestep within the trajectory
        :rtype Dict[str, torch.Tensor]: The data used for training
        """

        with MPNDataset(self._database, "r") as f:
            pidx = f[self.trajectory_key].lookup_pidx(idx)
            problem = f[self.trajectory_key].problem(pidx)
            flobs = f[self.trajectory_key].flattened_obstacles(pidx)
            item = self.get_inputs(problem, flobs)
            configs = f[self.trajectory_key].state_range(
                idx, lookahead=self.action_chunk_length + 1
            )
            config = configs[0]
            supervision = configs[1:]
            config_tensor = torch.as_tensor(config).float()

            if self.train:
                # Add slight random noise to the joints
                randomized = (
                    self.random_scale * torch.randn(config_tensor.shape) + config_tensor
                )

                item["configuration"] = self.clamp_and_normalize(randomized)
                robot_points = self.franka_sampler.sample(
                    randomized.numpy(), self.prismatic_joint
                )[:, :3]
            else:
                item["configuration"] = self.clamp_and_normalize(config_tensor)
                robot_points = self.franka_sampler.sample(
                    config_tensor.numpy(), self.prismatic_joint
                )[:, :3]
            robot_points = torch.as_tensor(robot_points).float()
            item["point_cloud"] = torch.cat((robot_points, item["point_cloud"]), dim=0)
            item["point_cloud_labels"] = torch.cat(
                (
                    torch.zeros(len(robot_points), 1),
                    item["point_cloud_labels"],
                )
            )

            item["idx"] = torch.as_tensor(idx)
            supervision_tensor = torch.as_tensor(supervision).float()
            item["supervision"] = self.clamp_and_normalize(supervision_tensor)

        return item


class DataModule(pl.LightningDataModule):
    def __init__(
        self,
        data_dir: str,
        train_trajectory_key: str,
        val_trajectory_key: str,
        num_robot_points: int,
        num_obstacle_points: int,
        num_target_points: int,
        prismatic_joint: float,
        action_chunk_length: int,
        random_scale: float,
        train_batch_size: int,
        val_batch_size: int,
        num_workers: int,
        ignore_pretrain_data: bool,
    ):
        super().__init__()
        self.data_dir = Path(data_dir)
        self.train_trajectory_key = train_trajectory_key
        self.val_trajectory_key = val_trajectory_key
        self.train_batch_size = train_batch_size
        self.val_batch_size = val_batch_size
        self.num_robot_points = num_robot_points
        self.num_obstacle_points = num_obstacle_points
        self.num_target_points = num_target_points
        self.num_workers = num_workers
        self.prismatic_joint = prismatic_joint
        self.random_scale = random_scale
        self.ignore_pretrain_data = ignore_pretrain_data
        self.action_chunk_length = action_chunk_length

    def setup(self, stage: Optional[str] = None):
        """
        A Pytorch Lightning method that is called per-device in when doing
        distributed training.

        :param stage Optional[str]: Indicates whether we are in the training
                                    procedure or if we are doing ad-hoc testing
        """
        if stage == "fit" or stage is None:
            self.data_train = StateDataset.load_from_directory(
                self.data_dir,
                dataset_type=DatasetType.TRAIN,
                trajectory_key=self.train_trajectory_key,
                num_robot_points=self.num_robot_points,
                num_obstacle_points=self.num_obstacle_points,
                num_target_points=self.num_target_points,
                prismatic_joint=self.prismatic_joint,
                random_scale=self.random_scale,
                action_chunk_length=self.action_chunk_length,
            )
            self.data_val_state = StateDataset.load_from_directory(
                self.data_dir,
                dataset_type=DatasetType.VAL_STATE,
                trajectory_key=self.val_trajectory_key,
                num_robot_points=self.num_robot_points,
                num_obstacle_points=self.num_obstacle_points,
                num_target_points=self.num_target_points,
                prismatic_joint=self.prismatic_joint,
                random_scale=0.0,
                action_chunk_length=self.action_chunk_length,
            )
            self.data_val = TrajectoryDataset.load_from_directory(
                self.data_dir,
                dataset_type=DatasetType.VAL,
                trajectory_key=self.val_trajectory_key,
                num_robot_points=self.num_robot_points,
                num_obstacle_points=self.num_obstacle_points,
                num_target_points=self.num_target_points,
                prismatic_joint=self.prismatic_joint,
                random_scale=0.0,
            )
            self.data_mini_train = TrajectoryDataset.load_from_directory(
                self.data_dir,
                dataset_type=DatasetType.MINI_TRAIN,
                trajectory_key=self.val_trajectory_key,
                num_robot_points=self.num_robot_points,
                num_obstacle_points=self.num_obstacle_points,
                num_target_points=self.num_target_points,
                prismatic_joint=self.prismatic_joint,
                random_scale=0.0,
            )
            self.data_val_pretrain = TrajectoryDataset.load_from_directory(
                self.data_dir,
                dataset_type=DatasetType.VAL_PRETRAIN,
                trajectory_key=self.val_trajectory_key,
                num_robot_points=self.num_robot_points,
                num_obstacle_points=self.num_obstacle_points,
                num_target_points=self.num_target_points,
                prismatic_joint=self.prismatic_joint,
                random_scale=0.0,
            )
        if stage == "test" or stage is None:
            self.data_test = StateDataset.load_from_directory(
                self.data_dir,
                self.train_trajectory_key,  # TODO change this
                self.num_robot_points,
                self.num_obstacle_points,
                self.num_target_points,
                dataset_type=DatasetType.TEST,
                prismatic_joint=self.prismatic_joint,
                random_scale=self.random_scale,
                action_chunk_length=self.action_chunk_length,
            )
        if stage == "dagger":
            self.data_dagger = TrajectoryDataset.load_from_directory(
                self.data_dir,
                dataset_type=DatasetType.TRAIN,
                trajectory_key=self.val_trajectory_key,
                num_robot_points=self.num_robot_points,
                num_obstacle_points=self.num_obstacle_points,
                num_target_points=self.num_target_points,
                prismatic_joint=self.prismatic_joint,
                random_scale=0.0,
            )

    def train_dataloader(self) -> DataLoader:
        """
        A Pytorch lightning method to get the dataloader for training

        :rtype DataLoader: The training dataloader
        """
        return DataLoader(
            self.data_train,
            self.train_batch_size,
            num_workers=self.num_workers,
            pin_memory=True,
            shuffle=True,
        )

    def dagger_dataloader(self) -> DataLoader:
        return DataLoader(
            self.data_dagger,
            self.val_batch_size,
            num_workers=self.num_workers,
            pin_memory=True,
            shuffle=True,
        )

    def val_dataloader(self) -> DataLoader:
        """
        A Pytorch lightning method to get the dataloader for validation

        :rtype DataLoader: The validation dataloader
        """
        loaders = [None, None, None, None]
        loaders[DatasetType.VAL_STATE] = DataLoader(
            self.data_val_state,
            self.train_batch_size,  # Set this way because this dataset matches the structure of the training
            num_workers=self.num_workers,
            pin_memory=True,
        )
        loaders[DatasetType.VAL] = DataLoader(
            self.data_val,
            self.val_batch_size,
            num_workers=self.num_workers,
            pin_memory=True,
        )
        loaders[DatasetType.MINI_TRAIN] = DataLoader(
            self.data_mini_train,
            self.val_batch_size,
            num_workers=self.num_workers,
            pin_memory=True,
        )
        # Done this way to keep indexing safety logic
        loaders[DatasetType.VAL_PRETRAIN] = DataLoader(
            self.data_val_pretrain,
            self.val_batch_size,
            num_workers=self.num_workers,
            pin_memory=True,
        )
        return loaders

    def test_dataloader(self) -> DataLoader:
        """
        A Pytorch lightning method to get the dataloader for testing

        :rtype DataLoader: The dataloader for testing
        """
        assert NotImplementedError("Not implemented")

    def md5_checksums(self):
        """
        Currently too lazy to figure out how to fit this into Lightning with the whole
        setup() thing and the data being initialized in that call and when to get
        hyperparameters etc etc, so just hardcoding the paths right now
        """
        paths = [
            ("train", self.data_dir / "train" / "train.hdf5"),
            ("val", self.data_dir / "val" / "val.hdf5"),
            ("mini_train", self.data_dir / "val" / "mini_train.hdf5"),
        ]
        checksums = {}
        for key, path in paths:
            if path.exists():
                with MPNDataset(path) as f:
                    checksums[key] = f.md5_checksum
        return checksums
