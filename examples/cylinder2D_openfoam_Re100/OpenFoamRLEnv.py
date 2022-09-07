# from cmath import inf
from cmath import acos
from signal import signal
import subprocess
import argparse
from tkinter.messagebox import NO
import numpy as np
import math
import precice
from precice import action_write_initial_data, action_write_iteration_checkpoint
from pathlib import Path
from datetime import datetime
import os

import gym
from gym import spaces
from mesh_parser import FoamMesh

import time
import psutil
from utils import get_cfg_data, parse_probe_lines, make_parallel_config, load_file, parallel_precice_dict, find_interface_patches, open_file, get_coupling_data
from utils import robust_readline
import utils
import xmltodict
import copy
import pandas as pd

from os.path import join
import os

# def unload_module(module_name):
#     import sys
#     try:
#         del sys.modules[module_name]
#     except Exception as e:
#         print(e)
#         pass


class OpenFoamRLEnv(gym.Env):
    """
    RL Env via a dummy precice adapter class
    standard OpenAI gym functions (step, reset, close, render)
    internal functions starts with '_' (e.g. _make_run_folders)
    problem setup functions starts with setup_ (e.g. setup_env_obs_act)
    """
    metadata = {"render_modes": [], "render_fps": 4}

    def __init__(self, options) -> None:
        super().__init__()

        self.__options = copy.deepcopy(options)

        # gym env attributes:
        self.__is_reset = False
        #self.__prerun_needed = None
        self.__prerun_needed = self.__options.get("prerun_needed", False)
        self.__is_first_reset = True  # if True, gym env reset has been called at least once
        # action_ and observation_space will be set in _set_precice_data
        self.action_space = None
        self.observation_space = None
        self.__is_data_initialized = None

        self.__patch_data = None

        max_time, _ = get_coupling_data('', self.__options['precice_cfg']) 
        self.__init_max_time = float(max_time)

        self.run_folders = self._make_run_folders()

        scaler_variables, vector_variables, mesh_list, mesh_variables = \
            get_cfg_data('', self.__options['precice_cfg'])


        print(scaler_variables, vector_variables, mesh_list, mesh_variables)
        # select only RL-Gym meshes
        mesh_list = [x for x in mesh_list if 'rl' in x.lower()]
        self.__mesh_variables = mesh_variables
        self.__mesh_list = mesh_list

        # scaler and vector variables should be used to define the size of action space
        self.scaler_variables = scaler_variables
        self.vector_variables = vector_variables

        # coupling attributes:
        self.__precice_dt = None
        self.__interface = None  # preCICE interface
        self.__time_window = None
        self.__t = None
        self.__prerun_t = None
        self.__mesh_id = None
        self.__dim = None
        self.__vertex_ids = None

        # solver attributes:
        self.__solver_run = []  # physical solver
        self.__solver_full_reset = False  # if True, run foam-preprocess upon every reset

        # observations and rewards are obtained from post-processing files
        self.__probes_rewards_data = {}
        self.__postprocessing_filehandler_dict = {}
        self.__precice_read_data = {}
        
        
        #self.setup_env_obs_act()
        self.action_space = spaces.Box(
            low=-0.00001, high=0.00001, shape=(1, ), dtype=np.float64)
        self.observation_space = spaces.Box(
            low=-np.inf, high=np.inf, shape=(11,), dtype=np.float64)

    def __del__(self):
        # close all the open files
        self._close_postprocessing_files()
        if self.__interface:
            try:
                print('crashing the interface using negative timestep')
                self.__interface.advance(-1)
                # another idea is to advance with random actions till the coupling finish and finalize
                # self.__interface.finalize()
            except Exception as e:
                pass
        # print('delete the interface')
        # self.__interface.finalize()

    def _make_run_folders(self):
        # 1- clean the case file
        # 2- run grid preprocessor on the original OpenFoam case files
        # 3- parse mesh data from the case folder to get the rl-grid
        # 4- duplicate xml file for parallel processing
        # 5- replicate the folders using a mix of symbolic links and modified preciceDict

        shell_cmd = self.__options.get("foam_shell_cmd", "")
        case_path = self.__options.get("case_path", "")
        clean_cmd = self.__options.get("clean_cmd", "")
        preprocess_cmd = self.__options.get("preprocess_cmd", "")
        prerun_needed = self.__options.get("prerun_needed", False)
        prerun_time = self.__options.get("prerun_time", 0.0)
        prerun_cmd = self.__options.get("prerun_cmd", "")

        self._launch_subprocess(shell_cmd, clean_cmd, case_path, cmd_type='clean')
        self._launch_subprocess(shell_cmd, preprocess_cmd, case_path, cmd_type='preprocess')
        if prerun_needed:
            print("Wait: non-controlled OF is running ...")
            self._launch_subprocess(shell_cmd, prerun_cmd, case_path, cmd_type='prerun')

        # Create an empty folder for the RL_Gym to run OpenFoam
        cwd = Path.cwd()
        time_str = datetime.now().strftime('%d%m%Y_%H%M%S')
        run_folder_name = f'rl_gym_run_{time_str}'
        run_folder = cwd.joinpath(run_folder_name)
        try:
            run_folder.mkdir(parents=True, exist_ok=True)
        except Exception as e:
            raise Exception(f'failed to create run folder: {e}')

        case_path_str = self.__options['case_path']
        source_folder_str = join(str(cwd), case_path_str)

        # parse solver mesh data
        patch_list = find_interface_patches(load_file(join(source_folder_str, 'system'), 'preciceDict'))
        self.__patch_data = self.setup_mesh_data(case_path, patch_list)

        # Create a n_parallel case_folders as symbolic links

        run_folder_list = []

        for idx_ in range(self.__options['n_parallel_env']):
            dist_folder_str = str(run_folder) + '/' + case_path_str + f'_{idx_}'
            run_folder_list.append(dist_folder_str)
            try:
                os.system(f'cp -rs {source_folder_str} {dist_folder_str}')
            except Exception as e:
                raise Exception(f'Failed to create symbolic links to foam case files: {e}')

            precicedict_str = load_file(dist_folder_str + "/system/", 'preciceDict')
            # print(precicedict_str)
            new_string = parallel_precice_dict(precicedict_str, idx_)
            # print(new_string)
            # delete the symbolic link
            complete_filepath = f'{dist_folder_str}/system/preciceDict'

            os.system(f"rm {complete_filepath}")
            with open(complete_filepath, 'w') as file_obj:
                file_obj.write(new_string)

        # Get a new version of precice-config.xml
        parallel_tree = make_parallel_config(str(cwd) + '/', "precice-config.xml", self.__options['n_parallel_env'], run_folder_list, use_mapping=True)
        precice_config_parallel_file = str(run_folder) + "/precice-config.xml"

        with open(precice_config_parallel_file, 'w') as file_obj:
            file_obj.write(xmltodict.unparse(parallel_tree, encoding='utf-8', pretty=True))

        os.chdir(str(run_folder))
        os.system(f'cp ../{shell_cmd} .')

        # we don't want symbolic links to the paraview files
        # os.system(f'rm -f {dist_folder_str}/case.foam')
        # os.system(f'rm -f {dist_folder_str}/case.OpenFOAM')

        # os.system(f'cp {source_folder_str}/case.foam {dist_folder_str}/case.foam')
        # os.system(f'cp {source_folder_str}/case.OpenFOAM {dist_folder_str}/case.OpenFOAM')

        self.__options['precice_cfg'] = precice_config_parallel_file
        return run_folder_list

    # gym methods:
    def reset(self, *, seed=None, return_info=False):
        super().reset(seed=seed)

        # get the solver-launch options
        shell_cmd = self.__options.get("foam_shell_cmd", "")
        case_path = self.__options.get("case_path", "")
        clean_cmd = self.__options.get("clean_cmd", "")
        softclean_cmd = self.__options.get("softclean_cmd", "")
        preprocess_cmd = self.__options.get("preprocess_cmd", "")
        prerunclean_cmd = self.__options.get("prerunclean_cmd", "")
        run_cmd = self.__options.get("run_cmd", "")
        self.__solver_full_reset = self.__options.get("solver_full_reset", self.__solver_full_reset)


        
        self.__max_time = self.__init_max_time

        for p_idx in range(self.__options['n_parallel_env']):
            p_case_path = case_path + f'_{p_idx}'
            # clean the log files
            if self.__prerun_needed:
                self._launch_subprocess(shell_cmd, prerunclean_cmd, p_case_path, cmd_type='prerunclean')
            else:
                self._launch_subprocess(shell_cmd, softclean_cmd, p_case_path, cmd_type='softclean')

        if len(self.__postprocessing_filehandler_dict) > 0:
            self._close_postprocessing_files()

        # run open-foam solver
        if len(self.__solver_run) > 0:
            raise Exception('solver_run pointer is not cleared -- should not reach here')

        for p_idx in range(self.__options['n_parallel_env']):
            p_case_path = case_path + f'_{p_idx}'
            if self.__options['is_dummy_run']:
                p_process = self._launch_dummy_subprocess(p_idx, p_case_path)
            else:
                p_process = self._launch_subprocess(shell_cmd, run_cmd, p_case_path, cmd_type='run')

            assert p_process is not None
            self.__solver_run.append(p_process)

        # checking spawning after n_parallel calls to avoid sleeping $n times
        time.sleep(0.5)  # single wait time for all parallel runs
        for p_idx in range(self.__options['n_parallel_env']):
            p_case_path = case_path + f'_{p_idx}'
            self._check_subprocess(self.__solver_run[p_idx], shell_cmd, run_cmd, p_case_path, cmd_type='run')

        # initiate precice interface and read single mesh data
        self._init_precice()
        self._set_precice_data()

        if self.__interface.is_action_required(action_write_initial_data()):
            # what is the action for this case no action have been provided
            # TODO: what is the first action before we can do this reliably
            # self.__write_data = {}
            # for p_idx in range(self.__options['n_parallel_env']):
            #     initial_action = self.setup_initial_action(p_idx)
            #     conv_action = self.setup_patch_field_to_write(initial_action, self.__patch_data)  # TODO: self.__patch_data to be moved into setup
            #     actions_dict = self.setup_action_to_write_data(conv_action, p_idx)

            #     self.__write_data.update(actions_dict)
            # self._write()
            self.__interface.mark_action_fulfilled(action_write_initial_data())

        t0 = time.time()
        if not self.__prerun_needed:
            self.__interface.initialize_data()  # if initialize="True" --> push solver one time-window forward
            self.__t = self.__precice_dt
            self.__is_data_initialized = True
        else:
            self.__is_data_initialized = False

        print(f"RL-gym self.__interface.initialize_data() done in {time.time()-t0} seconds")
        # this results in reading data ahead of time when this is participant 2
        if self.__interface.is_read_data_available():
            self._read()

        self.__is_reset = True
        self.__is_first_reset = False
       
        self.__probes_rewards_data = {}
        self.__precice_read_data = {}

        # self._read_observations()  # read observation from probe files
        if self.__prerun_needed:
            self._read_prerun_probes_rewards_files()
            self.__t = self.__prerun_t
            self.__max_time += self.__prerun_t
        else:
            self._read_probes_rewards_files()
            self.__prerun_t = self.__precice_dt


        obs_list = self.setup_observations(n_lookback=0)

        # obs_np = self.setup_observations()

        # if self.__options['n_parallel_env'] == 1:
        #     obs_np = obs_np[0]
        # if return_info:
        #     return obs_np, {}
        # return obs_np

        if return_info:
            return obs_list, {}
        return obs_list

    def step(self, action):
        t0 = time.time()
        # if not isinstance(action, list) and not isinstance(action, np.ndarray):
        #     raise Exception("Action should be either a list or numpy array")
        # if isinstance(action, np.ndarray) and len(action.shape) == 2 and action.shape[0] == 1:
        #     action = action[0, :]

        if not self.__is_reset:
            raise Exception("Call reset before interacting with the environment.")

        if self.__interface.is_action_required(
                action_write_iteration_checkpoint()):
            self.__interface.mark_action_fulfilled(
                action_write_iteration_checkpoint())

        self.__write_data = {}

        if self.__options['n_parallel_env'] > 1:
            action = action.squeeze().tolist()
        else:
            action = action[0]
            
        # dummy random values to be sent to the solver
        for p_idx in range(self.__options['n_parallel_env']):
            conv_action = self.setup_patch_field_to_write(action[p_idx], self.__patch_data)  # TODO: self.__patch_data to be moved into setup
            actions_dict = self.setup_action_to_write_data(conv_action, p_idx)
            self.__write_data.update(actions_dict)

        t1 = time.time()

        # print('inside step function --- just before advance===========================')
        # print(self.__write_data)

        if not self.__is_data_initialized:
            self._initialize()
            self.__is_data_initialized = True
        else:
            self._advance()  # run fluid solver to complete the next time-window
        t2 = time.time()

        # self._read_observations()  # read observation from probe files
        self._read_probes_rewards_files()
        t3 = time.time()


        # observations = self.setup_observations()
        # rewards = self.setup_reward()

        # t4 = time.time()
        # print(f'Inside step function run times, write data: {t1-t0}, advance: {t2-t1}, read data: {t3-t2}, process read data: {t4-t3}')
        # done = not self.__interface.is_coupling_ongoing()
        # # delete precice object upon done (a workaround to get precice reset)

        # if done:
        #     self.__interface.finalize()
        #     del self.__interface
        #     print("preCICE finalized...\n")
        #     # we need to check here that solver run is finalized
        #     self.__solver_run = self._finalize_subprocess(self.__solver_run)

        #     # reset pointers
        #     self.__interface = None
        #     self.__solver_full_reset = False
        #     dones = [True] * rewards.shape[0]
        # else:
        #     dones = [False] * rewards.shape[0]

        # if self.__options['n_parallel_env'] == 1:
        #     observations = observations[0]
        #     rewards = rewards[0]
        #     dones = dones[0]

        # return observations, rewards, dones, {}


        obs_list = self.setup_observations()
        reward = self.setup_reward()
        t4 = time.time()
        print(f'Inside step function run times, write data: {t1-t0}, advance: {t2-t1}, read data: {t3-t2}, process read data: {t4-t3}')
        done = not self.__interface.is_coupling_ongoing()
        # delete precice object upon done (a workaround to get precice reset)
        if done:
            self.__interface.finalize()
            del self.__interface
            print("preCICE finalized...\n")
            # we need to check here that solver run is finalized
            self.__solver_run = self._finalize_subprocess(self.__solver_run)

            # reset pointers
            self.__interface = None
            self.__solver_full_reset = False
            self.__is_reset = False

        return obs_list, reward, done, {}

    def render(self, mode='human'):
        """ not implemented """
        if mode == "human":
            pass

    def close(self):
        """ not implemented """

    # preCICE related methods:
    def _init_precice(self):

        if self.__interface:
            raise Exception("precice interface already initialized, we should not reach here in normal situations")
        self.__interface = precice.Interface("RL-Gym", self.__options['precice_cfg'], 0, 1)

        self.__time_window = 0

        self.__dim = self.__interface.get_dimensions()

        self.__mesh_id = {}
        self.__vertex_coords = {}
        self.__vertex_ids = {}
        for mesh_name in self.__mesh_list:
            mesh_id = self.__interface.get_mesh_id(mesh_name)
            print(f"Mesh_ID: {mesh_id}, Mesh_name: {mesh_name}")
            # bounding_box = [-np.inf, np.inf] * self.__dim
            # self.__interface.set_mesh_access_region(mesh_id, bounding_box)  # ERROR:  setMeshAccessRegion may only be called once.
            self.__mesh_id[mesh_name] = mesh_id

            # TODO: not in use - for now we use a shared global grid
            # vertex_coords = self.setup_mesh_coords(mesh_name)

            # vertex_coords = np.zeros([5, self.__dim])
            # TODO: a separate function to deal with patch_integrate geometric data
            Cf = []
            for patch_name in self.__patch_data.keys():
                Cf.append(self.__patch_data[patch_name]['Cf'])
            vertex_coords = np.array([item for sublist in Cf for item in sublist])

            vertex_ids = self.__interface.set_mesh_vertices(mesh_id, vertex_coords)
            self.__vertex_ids[mesh_name] = vertex_ids
            self.__vertex_coords[mesh_name] = vertex_coords
        # establish connection with the solver
        self.__precice_dt = self.__interface.initialize() # if initialize="False" --> run solver to complete one time-window 

    def _advance(self):
        self._write()
        self.__interface.advance(self.__precice_dt)
        # increase the time before reading the probes/forces for internal consistency checks
        if self.__interface.is_time_window_complete():
            self.__time_window += 1
        self.__t += self.__precice_dt

        # dummy advance to finalize time-window and coupling status
        if math.isclose(self.__t, self.__max_time):
            self.__interface.advance(self.__precice_dt)
    
    def _initialize(self):
        self._write()
        self.__interface.initialize_data()#self.__interface.advance(self.__precice_dt)
        # increase the time before reading the probes/forces for internal consistency checks
        self.__time_window += 1
        self.__t += self.__precice_dt

    def _set_precice_data(self):
        self.__read_ids = {}
        self.__write_ids = {}

        # precice data from a single mesh on the solver side
        for mesh_name in self.__mesh_list:
            try:
                read_var_list = self.__mesh_variables[mesh_name]['read']
            except Exception as e:
                # a valid situation when the mesh doesn't have any read variables
                read_var_list = []
            try:
                write_var_list = self.__mesh_variables[mesh_name]['write']
            except Exception as e:
                # a valid situation when the mesh doesn't have any write variables
                write_var_list = []

            for read_var in read_var_list:
                self.__read_ids[read_var] = self.__interface.get_data_id(read_var, self.__mesh_id[mesh_name])

            for write_var in write_var_list:
                self.__write_ids[write_var] = self.__interface.get_data_id(write_var, self.__mesh_id[mesh_name])

    def _read(self):
        if self.__interface.is_read_data_available():
            pass
            # for mesh_name in self.__mesh_list:
            #     try:
            #         read_var_list = self.__mesh_variables[mesh_name]['read']
            #     except Exception as e:
            #         read_var_list = []
            #     for read_var in read_var_list:
            #         if read_var in self.vector_variables:
            #             self.__precice_read_data[read_var] = self.__interface.read_block_vector_data(
            #                 self.__read_ids[read_var], self.__vertex_ids[mesh_name])
            #         else:
            #             self.__precice_read_data[read_var] = self.__interface.read_block_scalar_data(
            #                 self.__read_ids[read_var], self.__vertex_ids[mesh_name])
            #         print(f"(RL-Gym), avg-{read_var} using {mesh_name} read = {self.__precice_read_data[read_var].mean():.4f}")
            #         print("-------------------------------------------")

    def _write(self):
        for mesh_name in self.__mesh_list:
            try:
                write_var_list = self.__mesh_variables[mesh_name]['write']
            except Exception as e:
                write_var_list = []
            for write_var in write_var_list:
                if write_var in self.vector_variables:
                    self.__interface.write_block_vector_data(
                        self.__write_ids[write_var], self.__vertex_ids[mesh_name], self.__write_data[write_var])
                else:
                    self.__interface.write_block_scalar_data(
                        self.__write_ids[write_var], self.__vertex_ids[mesh_name], self.__write_data[write_var])
                print(f"(RL-Gym), avg-{write_var} using {mesh_name} write = {self.__write_data[write_var].mean():.4f}")
                print("-------------------------------------------")

    def _launch_dummy_subprocess(self, process_idx, cwd):
        cmd_str = f'python -u fluid-solver.py'
        subproc = subprocess.Popen(cmd_str, shell=True, cwd=cwd)
        return subproc

    def _launch_subprocess(self, shell_cmd, cmd, cwd, cmd_type):
        cmd_str = f'. ../{shell_cmd} {cmd}'  # here we have relative path
        if cmd_type in ['clean', 'softclean', 'prerunclean']:
            print(cmd_type, cmd_str, cwd, os.getcwd())
            print("================")
            try:
                completed_process = subprocess.run(cmd_str, shell=True, cwd=cwd)
            except Exception as e:
                print(e)
                raise Exception(f'failed to run {cmd_type}: {cmd_str} from the folder {cwd}')

            if completed_process.returncode != 0:
                raise Exception(f"run is not successful: {completed_process}")
            return None
        elif cmd_type in ['preprocess', 'prerun']:
            # preprocess on the main folder before the symbolic links
            print(cmd_type, cmd_str, cwd, os.getcwd())
            print("================")
            completed_process = subprocess.run(cmd_str, shell=True, cwd=cwd)
            if completed_process.returncode != 0:
                raise Exception(f"run is not successful: {completed_process}")
            return None
        else:
            print(cmd_type, cmd_str, cwd, os.getcwd())
            print("================")
            subproc = subprocess.Popen(cmd_str, shell=True, cwd=cwd)
            return subproc

    def _check_subprocess(self, subproc, shell_cmd, cmd, cwd, cmd_type):
        cmd_str = f'. ../{shell_cmd} {cmd}'  # here we have relative path

        # check if the spawning process is successful
        if not psutil.pid_exists(subproc.pid):
            raise Exception(f'Error: subprocess failed to be launched {cmd_type}: {cmd_str} run from {cwd}')

        # finalize the subprocess if it is terminated (normally/abnormally)
        if psutil.Process(subproc.pid).status() == psutil.STATUS_ZOMBIE:
            print(psutil.Process(subproc.pid), psutil.Process(subproc.pid).status())
            raise Exception(f'Error: subprocess failed to be launched  {cmd_type}: {cmd_str} STATUS_ZOMBIE run from {cwd}')

    def _finalize_subprocess(self, process_list):
        for subproc in process_list:
            if subproc and psutil.pid_exists(subproc.pid):
                if psutil.Process(subproc.pid).status() != psutil.STATUS_ZOMBIE:
                    print("subprocess status is not zombie - waiting to finish ...")
                    exit_signal = subproc.wait()
                else:
                    print("subprocess status is zombie - cleaning up ...")
                    exit_signal = subproc.poll()
                # check the subprocess exit signal
                if exit_signal != 0:
                    raise Exception("subprocess failed to complete its shell command: " + subproc.args)
                print("subprocess successfully completed its shell command: " + subproc.args)

                # # force to kill the subprocess if still around
                # self._kill_subprocess(subproc) #  not necessary, poll/wait should do the job!
        return []

    # def _read_observations(self):
    #     if not self.__options['is_dummy_run']:
    #         # read precice after reading the files to avoid a nasty bug because of slow reading from files
    #         self._read_probes_rewards_files()
    #         self._read()

    def define_env_obs_act(self):
        # TODO: this should be problem specific
        # 1) action space need to be generalized here
        # 2) action space is not equal to observation space
        # 3) some of variables are scaler and other are vector
        # 4) where is rewards -- it is communicated? or read from online file?

        self.__n = 0
        # TODO: hidden assumption rl_gym have one mesh
        for mesh_name in self.__mesh_list:
            self.__n += self.__vertex_coords[mesh_name].shape[0]

        self.__n = int(self.__n / self.__options['n_parallel_env'])  # TODO: ????

    def _get_probes_rewards_dict(self, type_str, n_lookback):
        if not self.__is_reset:
            raise Exception("Call reset before interacting with the environment.")

        # get the data within a time_window for computing reward
        if self.__time_window == 0:
            time_bound = [0, self.__prerun_t]
        else:
            time_bound = [(self.__time_window - n_lookback) * self.__precice_dt + self.__prerun_t , self.__time_window * self.__precice_dt + self.__prerun_t]
    
        data_dict = {}
        for field_ in self.__options['postprocessing_data'].keys():
            for p_idx in range(self.__options['n_parallel_env']):
                p_field_ = f'{field_}_{p_idx}'
                field_info = self.__options['postprocessing_data'][field_]
                if field_info['use'] == type_str and \
                        p_field_ in self.__probes_rewards_data.keys() and \
                        len(self.__probes_rewards_data[p_field_]) > 0:
                    # avoid the starting again and again from t0 by working in reverse order
                    full_data = self.__probes_rewards_data[p_field_][::-1]
                    data_per_trj = []
                    for data in full_data:
                        time_stamp = data[0]
                        if time_stamp <= time_bound[0]:
                            break

                        # if time_stamp > time_bound[1]:
                        #     continue
                        # else:
                        data_per_trj.append(data)


                    data_dict[p_field_] = data_per_trj[::-1]
                    # print(f"_get_probes_rewards_dict: {p_field_}")
                    # print(data_dict[p_field_])

        return data_dict

    def _get_observations_dict(self, n_lookback):
        return self._get_probes_rewards_dict("observation", n_lookback)

    def _get_reward_dict(self, n_lookback):
        return self._get_probes_rewards_dict("reward", n_lookback)

    def _read_probes_rewards_files(self):
        # sequential read of a single line (last line) of the file at each RL-Gym step

        for p_idx in range(self.__options['n_parallel_env']):
            p_case_path = self.__options['case_path'] + f'_{p_idx}'
            for field_ in self.__options['postprocessing_data'].keys():
                temp_filename = ""
                if self.__prerun_needed:
                    filename = f"{p_case_path}{self.__options['postprocessing_data'][field_]['output_file']}"
                    filename_split = filename.split('/')
                    filename_split[-2] = str(self.__prerun_t)
                    temp_filename = '/'.join(filename_split)
                else:
                    temp_filename = f"{p_case_path}{self.__options['postprocessing_data'][field_]['output_file']}"
                print(f'reading filename: {temp_filename}')

                if temp_filename not in self.__postprocessing_filehandler_dict.keys():
                    # file_object = open(temp_filename, 'r')
                    file_object = open_file(temp_filename)
                    self.__postprocessing_filehandler_dict[temp_filename] = file_object

                # data = np.loadtxt(temp_filename  , unpack=True, usecols=[0, 1, 3])
                time_idx = 0
                # utils.wait_for_file(temp_filename, sleep_time=0.1)
                n_fields_expected = self.__options['postprocessing_data'][field_]['size']

                while not math.isclose(time_idx, self.__t):  # read till the end of time-window             

                    while True:
                        is_comment, time_idx, n_probes, probe_data = \
                            robust_readline(self.__postprocessing_filehandler_dict[temp_filename], n_fields_expected, sleep_time=0.01)
                        if not is_comment and n_fields_expected == n_probes:
                            break

                    # print(f"time: {time_idx}, Number of probes {n_probes}, probes data {probe_data}")
                    p_field_ = f'{field_}_{p_idx}'
                    if p_field_ not in self.__probes_rewards_data.keys():
                        self.__probes_rewards_data[p_field_] = []
                    self.__probes_rewards_data[p_field_].append([time_idx, n_probes, probe_data])

                    # line_text = self.__postprocessing_filehandler_dict[temp_filename].readline()
                    # if line_text == "":
                    #     continue
                    # # assert len(line_text) > 0, 'read a single line but it is of length 0 !!'
                    # line_text = line_text.strip()
                    # if len(line_text) > 0:
                    #     try:
                    #         is_comment, time_idx, n_probes, probe_data = parse_probe_lines(line_text)
                    #         # print(is_comment, time_idx, n_probes, probe_data)
                    #     except Exception as e:
                    #         continue
                    #     if is_comment:
                    #         time_idx = 0
                    #         continue
                    #     # print(f"time: {time_idx}, Number of probes {n_probes}, probes data {probe_data}")
                    #     p_field_ = f'{field_}_{p_idx}'
                    #     if p_field_ not in self.__probes_rewards_data.keys():
                    #         self.__probes_rewards_data[p_field_] = []
                    #     self.__probes_rewards_data[p_field_].append([time_idx, n_probes, probe_data])

                assert math.isclose(time_idx, self.__t), f"probes/forces data should be at the same time as RL-Gym: {time_idx} vs {self.__t}"    
   
    def _read_prerun_probes_rewards_files(self):

        for p_idx in range(self.__options['n_parallel_env']):
            p_case_path = self.__options['case_path'] + f'_{p_idx}'
            for field_ in self.__options['postprocessing_data'].keys():

                temp_filename = f"{p_case_path}{self.__options['postprocessing_data'][field_]['output_file']}"
                print(f'reading filename: {temp_filename}')

                if temp_filename not in self.__postprocessing_filehandler_dict.keys():
                    # file_object = open(temp_filename, 'r')
                    file_object = open_file(temp_filename)
                    self.__postprocessing_filehandler_dict[temp_filename] = file_object

                # data = np.loadtxt(temp_filename  , unpack=True, usecols=[0, 1, 3])
                time_idx = 0

                while True:
                    line_text = self.__postprocessing_filehandler_dict[temp_filename].readline()
                    if line_text == "":
                        break
                    is_comment, time_idx, n_probes, probe_data = parse_probe_lines(line_text.strip())
                    if is_comment:
                        continue
                    
                    # print(f"time: {time_idx}, Number of probes {n_probes}, probes data {probe_data}")
                    p_field_ = f'{field_}_{p_idx}'
                    if p_field_ not in self.__probes_rewards_data.keys():
                        self.__probes_rewards_data[p_field_] = []
                    self.__probes_rewards_data[p_field_].append([time_idx, n_probes, probe_data])

        first_key = list(self.__options['postprocessing_data'].keys())[0] + '_0'
        self.__prerun_t = self.__probes_rewards_data[first_key][-1][0]
        
                
        for filename_ in self.__postprocessing_filehandler_dict.keys():
            file_object = self.__postprocessing_filehandler_dict[filename_]
            try:
                file_object.close()
            except Exception as e:
                print(f"error in closing probes/forces file: {e}")
                pass
    
    def _close_postprocessing_files(self):
        for filename_ in self.__postprocessing_filehandler_dict.keys():
            file_object = self.__postprocessing_filehandler_dict[filename_]
            try:
                file_object.close()
            except Exception as e:
                print(f"error in closing probes/forces file: {e}")
                pass
        self.__postprocessing_filehandler_dict = {}

    def setup_patch_field_to_write(self, action, patch_data):
        # TODO: this should be problem specific
        theta0 = [90, 270]
        w = [10, 10]
        theta0 = [math.radians(x) for x in theta0]
        w = [math.radians(x) for x in w]
        origin = np.array([0, 0, 0.005])
        radius = 0.05
        patch_flow_rate = [-action, action]
        U = []

        for idx, patch_name in enumerate(patch_data.keys()):
            print(f"Prescribed action: FlowRate = {patch_flow_rate[idx]:.7f} [m/s3] on {patch_name}")
            patch_ctr = np.array([radius * math.cos(theta0[idx]), radius * math.sin(theta0[idx]), origin[2]])
            magSf = patch_data[patch_name]['magSf']
            Sf = patch_data[patch_name]['Sf']
            Cf = patch_data[patch_name]['Cf']
            nf = patch_data[patch_name]['nf']
            w_patch = w[idx]
            # convert volumetric flow rate to a sinusoidal profile on the interface
            avg_U = patch_flow_rate[idx] / np.sum(magSf)

            d = (patch_ctr - origin) / (np.sqrt((patch_ctr - origin).dot((patch_ctr - origin))))

            U_patch = np.zeros((Cf.shape[0], 3))

            # estimated flow rate based on the sinusoidal profile
            Q_calc = 0
            for i, c in enumerate(Cf):
                r = (c - origin) / (np.sqrt((c - origin).dot((c - origin))))
                theta = math.acos(np.dot(r, d))
                U_patch[i] = avg_U * math.pi / 2 * math.cos(math.pi / w_patch * theta) * nf[i]
                Q_calc += U_patch[i].dot(Sf[i])

            # correct velocity profile to enforce mass conservation
            Q_err = patch_flow_rate[idx] - Q_calc
            U_err = Q_err / np.sum(magSf) * nf
            U_patch += U_err

            # return the velocity profile
            Q_final = 0
            for i, Uf in enumerate(U_patch):
                Q_final += Uf.dot(Sf[i])

            if math.isclose(Q_final, patch_flow_rate[idx]):
                print(f"Set flow rate = {Q_final:.7f} [m/s3] on the {patch_name} interface")
                U.append(U_patch)
            else:
                raise Exception('estimated velocity profile violates mass conservation')

        U_profile = np.array([item for sublist in U for item in sublist])

        return U_profile

    def setup_mesh_data(self, case_path, patches):  #TODO: check why it fails for binary data
        """ parse polyMesh to get interface points:"""
        t0 = time.time()
        print('starting to parse FoamMesh')
        foam_mesh = FoamMesh(case_path)
        print(f'Done to parsing FoamMesh in {time.time()-t0} seconds')

        # nodes = foam_mesh.boundary_face_nodes(b'interface')
        # self.__n = nodes.shape[0]
        # self.__grid = nodes

        # if len(self.__mesh_list) > 1:
        #     raise Exception('currently this only works for single mesh used for both observations and actions')
        patch_data = {}
        for patch in patches:
            Cf = foam_mesh.boundary_face_centres(patch.encode())        
            Sf, magSf, nf = foam_mesh.boundary_face_area(patch.encode())
            patch_data[patch] = {'Cf': Cf, 'Sf': Sf, 'magSf': magSf, 'nf': nf}
        return patch_data

    def setup_mesh_coords(self, mesh_name):  # TODO: not in use for now
        """ Problem specific function """
        return self.Cf

    def setup_env_obs_act(self):
        pass 
        # TODO: this should be problem specific
        # 1) action space need to be generalized here
        # 2) action space is not equal to observation space
        # 3) some of variables are scaler and other are vector
        # 4) where is rewards -- it is communicated? or read from online file?

        #self.__n = 0
        # for mesh_name in self.__mesh_list:
        #     self.__n += self.__vertex_coords[mesh_name].shape[0]
        # # TODO: this should work even if RL-Gym have more than one mesh (not tested)
        # self.__n = int(self.__n / self.__options['n_parallel_env'])
        # print(f'size of the grid is {self.__n}')

        # self.action_space = spaces.Box(
        #     low=-0.00001, high=0.00001, shape=(1, ), dtype=np.float64)
        # self.observation_space = spaces.Box(
        #     low=-np.inf, high=np.inf, shape=(11,), dtype=np.float64)

    def setup_action_to_write_data(self, action, p_idx=0):
        """ Problem specific function """
        """ return dummy random values for Velocity """
        write_data_dict = {f"Velocity_{p_idx}": action}
        return write_data_dict

    def setup_initial_action(self, p_idx):
        # should we setup this in side of the options?
        return 0.0

    def setup_observations(self, n_lookback=1):
        obs_dict = self._get_observations_dict(n_lookback)
        # now we print all of the observations to a list
        # obs_list = []
        # # we should be filtering here some of the columns only
        # for field_ in obs_dict.keys():
        #     obs_list.append([field_, obs_dict[field_]])
        # return obs_list

        # list container to store 'pressure' probes
        obs_list = []
        print("\n---------------------------------------")
        print("probes avg-pressure for:")
        for p_idx in range(self.__options['n_parallel_env']):
            dict_name = f'p_{p_idx}'
            # data = obs_dict[dict_name][-1]
            # obs_list.append(data[2])
            pressures = [[[x[0]] + x[2]] for x in obs_dict[dict_name]]
            pressures = np.array(pressures).squeeze()  # first column is time and the rest are the probes
            obs_list.append(pressures[-1, 1:])  # only the last timestep and remove the timestamp

            print(f'Trajectory#{p_idx}:')
            print(f'Time: {pressures[-1, 0]} --> p_avg: {np.mean(pressures[-1, 1:])}')

           
        return np.array(obs_list).tolist() # list of list

        # check the function _flatten_obs in stable_baseline3 for more general approach
        # https://stable-baselines3.readthedocs.io/en/master/_modules/stable_baselines3/common/vec_env/subproc_vec_env.html#SubprocVecEnv.reset

        #return np.stack(obs_list)


    def setup_reward(self,n_lookback=1, lookback_time=0.025):
        """ Problem specific function """

        lookback_time = 0.335  # 1/2.9850746268656714
        n_lookback = 14  # how many precice timesteps to cover the lookback time

        reward_dict = self._get_reward_dict(n_lookback=n_lookback)
        # list container to store 'force-based' reward per trajectory
        reward_list = []

        print("\n---------------------------------------")
        print("Cd and Cl data for:")
        for p_idx in range(self.__options['n_parallel_env']):

            var_name = f'forces_{p_idx}'
            data_list = reward_dict[var_name]

            Cd = np.array([[x[0], x[2][0]] for x in data_list]).squeeze()
            Cl = np.array([[x[0], x[2][2]] for x in data_list]).squeeze()

            print(f'Trajectory#{p_idx}:')
            print(f'Time: {Cd[0, 0]} --> Cd: {Cd[0, 1]}, Cl: {Cl[0, 1]}')
            print(f'Time: {Cd[-1, 0]} --> Cd: {Cd[-1, 1]}, Cl: {Cl[-1, 1]}')

            last_time = Cd[-1, 0]
            start_time = last_time - lookback_time
            # average is in-correct because if we might be using adaptive time-stepping
            Cd_uniform = np.interp(np.linspace(start_time, last_time, num=100, endpoint=True), Cd[:, 0], Cd[:, 1])
            Cl_uniform = np.interp(np.linspace(start_time, last_time, num=100, endpoint=True), Cl[:, 0], Cl[:, 1])
            reward_value = np.mean(Cd_uniform) + 0.2 * np.mean(Cl_uniform)
            # reward_value = np.mean(Cd[:, 1]) + 0.2 * np.mean(Cl[:, 1])
            reward_list.append(-reward_value)

            # Cd = []
            # Cl = []
            # for row in data_list:
            #     Cd.append(row[2][0])
            #     Cl.append(abs(row[2][2]))

            # print(f'Trajectory#{p_idx}:')
            # print(f'Time: {data_list[0][0]} --> Cd: {Cd[0]}, Cl: {Cl[0]}')
            # print(f'Time: {data_list[-1][0]} --> Cd: {Cd[-1]}, Cl: {Cl[-1]}')
            # reward_list.append(-np.mean(Cd) - 0.2 * np.mean(Cl))
        print("---------------------------------------\n")


        return np.array(reward_list).squeeze() # numpy array

        #return np.array(reward_list)

