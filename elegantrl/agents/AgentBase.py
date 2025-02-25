import os
import torch
import numpy as np
import numpy.random as rd
from copy import deepcopy
from torch.nn.utils import clip_grad_norm_
from torch import Tensor
from typing import List, Tuple, Union 
from collections import deque


class AgentBase:
    def __init__(self, net_dim: int, state_dim: int, action_dim: int, gpu_id: int = 0, args=None):
        """initialize
        replace by different DRL algorithms

        :param net_dim: the dimension of networks (the width of neural networks)
        :param state_dim: the dimension of state (the number of state vector)
        :param action_dim: the dimension of action (the number of discrete action)
        :param gpu_id: the gpu_id of the training device. Use CPU when cuda is not available.
        :param args: the arguments for agent training. `args = Arguments()`
        """

        self.gamma = getattr(args, 'gamma', 0.99)
        self.env_num = getattr(args, 'env_num', 1)
        self.num_layer = getattr(args, 'num_layer', 3)
        self.batch_size = getattr(args, 'batch_size', 128)
        self.action_dim = getattr(args, 'action_dim', 3)
        self.state_dim = getattr(args, 'state_dim', 10)
        self.repeat_times = getattr(args, 'repeat_times', 1.)
        self.reward_scale = getattr(args, 'reward_scale', 1.)
        self.lambda_critic = getattr(args, 'lambda_critic', 1.)
        self.learning_rate = getattr(args, 'learning_rate', 2 ** -15)
        self.clip_grad_norm = getattr(args, 'clip_grad_norm', 3.0)
        self.soft_update_tau = getattr(args, 'soft_update_tau', 2 ** -8)

        self.if_use_per = getattr(args, 'if_use_per', False)
        self.if_off_policy = getattr(args, 'if_off_policy', None)
        self.if_use_old_traj = getattr(args, 'if_use_old_traj', False)

        self.state = None  # assert self.state == (env_num, state_dim)
        self.device = torch.device(f"cuda:{gpu_id}" if (torch.cuda.is_available() and (gpu_id >= 0)) else "cpu")

        '''network'''
        act_class = getattr(self, "act_class", None)
        cri_class = getattr(self, "cri_class", None)
        self.act = act_class(net_dim, self.num_layer, state_dim, action_dim).to(self.device)
        self.cri = cri_class(net_dim, self.num_layer, state_dim, action_dim).to(self.device) \
            if cri_class else self.act

        '''optimizer'''
        self.learning_rate = args.learning_rate
        self.act_optimizer = torch.optim.Adam(self.act.parameters(), self.learning_rate)
        self.cri_optimizer = torch.optim.Adam(self.cri.parameters(), self.learning_rate) \
            if cri_class else self.act_optimizer

        '''target network'''
        self.if_act_target = args.if_act_target if hasattr(args, 'if_act_target') else \
            getattr(self, 'if_act_target', None)
        self.if_cri_target = args.if_cri_target if hasattr(args, 'if_cri_target') else \
            getattr(self, 'if_cri_target', None)
        self.act_target = deepcopy(self.act) if self.if_act_target else self.act
        self.cri_target = deepcopy(self.cri) if self.if_cri_target else self.cri

        """attribute"""
        if self.env_num == 1:
            self.explore_env = self.explore_one_env
        else:
            self.explore_env = self.explore_vec_env

        self.criterion = torch.nn.SmoothL1Loss(reduction="mean")

        """tracker"""
        self.reward_tracker = Tracker(args.tracker_len)
        self.step_tracker = Tracker(args.tracker_len)
        self.current_rewards = torch.zeros(self.env_num, dtype=torch.float32, device=self.device)
        self.current_lengths = torch.zeros(self.env_num, dtype=torch.float32, device=self.device)

    def explore_one_env(self, env, horizon_len: int, random_exploration=None) -> list:
        """
        Collect trajectories through the actor-environment interaction for a **single** environment instance.

        :param env: RL training environment. env.reset() env.step(). It should be a vector env.
        :param horizon_len: explored horizon_len number of step in env
        :return: `[traj, ]`
        `traj = [(state, reward, done, action, noise), ...]` for on-policy
        `traj = [(state, reward, done, action), ...]` for off-policy
        """
        traj_list = []
        last_dones = [0, ]
        state = self.state[0]

        i = 0
        done = False
        while i < horizon_len or not done:
            tensor_state = torch.as_tensor(state, dtype=torch.float32).unsqueeze(0)
            tensor_action = self.act.get_action(tensor_state.to(self.device)).detach().cpu()  # different
            next_state, reward, done, _ = env.step(tensor_action[0].numpy())  # different

            traj_list.append((tensor_state, reward, done, tensor_action))  # different

            i += 1
            state = env.reset() if done else next_state

        self.state[0] = state
        last_dones[0] = i
        return self.convert_trajectory(traj_list, last_dones)  # traj_list

    def explore_vec_env(self, env, horizon_len: int, random_exploration: bool) -> list:
        """
        Collect trajectories through the actor-environment interaction for a **vectorized** environment instance.

        :param env: RL training environment. env.reset() env.step(). It should be a vector env.
        :param horizon_len: explored horizon_len number of step in env
        """
        obs = torch.zeros((horizon_len * self.env_num, self.state_dim)).to(self.device)
        actions = torch.zeros((horizon_len * self.env_num, self.action_dim)).to(self.device)
        rewards = torch.zeros((horizon_len * self.env_num)).to(self.device)
        next_obs = torch.zeros((horizon_len * self.env_num, self.state_dim)).to(self.device)
        dones = torch.zeros((horizon_len * self.env_num)).to(self.device)

        state = self.state if self.if_use_old_traj else env.reset()
        done = torch.zeros(self.env_num).to(self.device)

        for i in range(horizon_len):
            start = i * self.env_num
            end = (i + 1) * self.env_num
            obs[start:end] = state
            dones[start:end] = done

            if random_exploration:
                action = torch.rand((self.env_num, self.action_dim), device=self.device) * 2 - 1
            else:
                action = self.act.get_action(state).detach()  # different
            next_state, reward, done, _ = env.step(action)  # different
            state = next_state

            actions[start:end] = action
            rewards[start:end] = reward
            next_obs[start:end] = next_state

            self.current_rewards += reward
            self.current_lengths += 1
            env_done_indices = torch.where(done == 1)
            self.reward_tracker.update(self.current_rewards[env_done_indices])
            self.step_tracker.update(self.current_lengths[env_done_indices])
            not_dones = 1.0 - done.float()
            self.current_rewards = self.current_rewards * not_dones
            self.current_lengths = self.current_lengths * not_dones

        self.state = state
        return (obs, actions, self.reward_scale * rewards.reshape(-1, 1), next_obs, dones.reshape(-1, 1)), horizon_len * self.env_num

    def update_net(self, buffer) -> tuple:
        return 0.0, 0.0

    def get_obj_critic(self, buffer, batch_size: int) -> (Tensor, Tensor):
        """
        Calculate the loss of networks with **uniform sampling**.

        :param buffer: the ReplayBuffer instance that stores the trajectories.
        :param batch_size: the size of batch data for Stochastic Gradient Descent (SGD).
        :return: the loss of the network and state.
        """
        with torch.no_grad():
            reward, done, action, state, next_state = buffer.sample_batch(batch_size)
            next_a = self.act_target(next_state)
            critic_targets: torch.Tensor = self.cri_target(next_state, next_a)
            (next_q, min_indices) = torch.min(critic_targets, dim=1, keepdim=True)
            q_label = reward + done * next_q

        q = self.cri(state, action)
        obj_critic = self.criterion(q, q_label)

        return obj_critic, state

    def optimizer_update(self, optimizer, objective):  # [ElegantRL 2021.11.11]
        """minimize the optimization objective via update the network parameters

        :param optimizer: `optimizer = torch.optim.SGD(net.parameters(), learning_rate)`
        :param objective: `objective = net(...)` the optimization objective, sometimes is a loss function.
        """
        optimizer.zero_grad()
        objective.backward()
        clip_grad_norm_(parameters=optimizer.param_groups[0]["params"], max_norm=self.clip_grad_norm)
        optimizer.step()

    def optimizer_update_amp(self, optimizer, objective):  # automatic mixed precision
        """minimize the optimization objective via update the network parameters

        amp: Automatic Mixed Precision

        :param optimizer: `optimizer = torch.optim.SGD(net.parameters(), learning_rate)`
        :param objective: `objective = net(...)` the optimization objective, sometimes is a loss function.
        """
        amp_scale = torch.cuda.amp.GradScaler()  # write in __init__()

        optimizer.zero_grad()
        amp_scale.scale(objective).backward()  # loss.backward()
        amp_scale.unscale_(optimizer)  # amp

        # from torch.nn.utils import clip_grad_norm_
        clip_grad_norm_(parameters=optimizer.param_groups[0]["params"], max_norm=self.clip_grad_norm)
        amp_scale.step(optimizer)  # optimizer.step()
        amp_scale.update()  # optimizer.step()

    @staticmethod
    def soft_update(target_net, current_net, tau: float):
        """soft update target network via current network

        :param target_net: update target network via current network to make training more stable.
        :param current_net: current network update via an optimizer
        :param tau: tau of soft target update: `target_net = target_net * (1-tau) + current_net * tau`
        """
        for tar, cur in zip(target_net.parameters(), current_net.parameters()):
            tar.data.copy_(cur.data * tau + tar.data * (1.0 - tau))

    
    def convert_trajectory(
            self, traj_list: List[Tuple[Tensor, ...]],
            last_done: Union[Tensor, list]
    ) -> List[Tensor]:
        # assert len(buf_items[0]) in {4, 5}
        # assert len(buf_items[0][0]) == self.env_num
        traj_list1 = list(map(list, zip(*traj_list)))  # state, reward, done, action, noise
        del traj_list
        # assert len(buf_items[0]) == step
        # assert len(buf_items[0][0]) == self.env_num

        '''stack items'''
        traj_state = torch.stack(traj_list1[0])
        traj_action = torch.stack(traj_list1[3])

        if len(traj_action.shape) == 2:
            traj_action = traj_action.unsqueeze(2)

        if self.env_num > 1:
            traj_reward = (torch.stack(traj_list1[1]) * self.reward_scale).unsqueeze(2)
            traj_mask = ((1 - torch.stack(traj_list1[2])) * self.gamma).unsqueeze(2)
        else:
            traj_reward = (torch.tensor(traj_list1[1], dtype=torch.float32) * self.reward_scale).reshape(-1, 1, 1)
            traj_mask = ((1 - torch.tensor(traj_list1[2], dtype=torch.float32)) * self.gamma).reshape(-1, 1, 1)

        if len(traj_list1) <= 4:
            traj_list2 = [traj_state, traj_reward, traj_mask, traj_action]
        else:
            traj_noise = torch.stack(traj_list1[4])
            traj_list2 = [traj_state, traj_reward, traj_mask, traj_action, traj_noise]
        del traj_list1

        '''splice items'''
        traj_list3 = list()
        for j in range(len(traj_list2)):
            cur_item = list()
            buf_item = traj_list2[j]

            for env_i in range(self.env_num):
                last_step = last_done[env_i]

                pre_item = self.traj_list[env_i][j]
                if len(pre_item):
                    cur_item.append(pre_item)

                cur_item.append(buf_item[:last_step, env_i])

                if self.if_use_old_traj:
                    self.traj_list[env_i][j] = buf_item[last_step:, env_i]

            traj_list3.append(torch.vstack(cur_item))
        del traj_list2
        # on-policy:  buf_item = [states, rewards, dones, actions, noises]
        # off-policy: buf_item = [states, rewards, dones, actions]
        # buf_items = [buf_item, ...]
        return traj_list3

    def save_or_load_agent(self, cwd: str, if_save: bool):
        """save or load training files for Agent

        :param cwd: Current Working Directory. ElegantRL save training files in CWD.
        :param if_save: True: save files. False: load files.
        """

        def load_torch_file(model, path: str):
            state_dict = torch.load(path, map_location=lambda storage, loc: storage)
            model.load_state_dict(state_dict)

        name_obj_list = [
            ("actor", self.act),
            ("act_target", self.act_target),
            ("act_optimizer", self.act_optimizer),
            ("critic", self.cri),
            ("cri_target", self.cri_target),
            ("cri_optimizer", self.cri_optimizer),
        ]
        name_obj_list = [(name, obj) for name, obj in name_obj_list if obj is not None]

        if if_save:
            for name, obj in name_obj_list:
                save_path = f"{cwd}/{name}.pth"
                torch.save(obj.state_dict(), save_path)
        else:
            for name, obj in name_obj_list:
                save_path = f"{cwd}/{name}.pth"
                load_torch_file(obj, save_path) if os.path.isfile(save_path) else None
    def get_buf_h_term_k(
            self, buf_state: Tensor, buf_action: Tensor, buf_mask: Tensor, buf_reward: Tensor
    ):
        buf_dones = torch.where(buf_mask == 0)[0].detach().cpu() + 1
        i = 0
        for j in buf_dones:
            rewards = buf_reward[i:j].to(torch.float16)
            states = buf_state[i:j].to(torch.float16)
            actions = buf_action[i:j].to(torch.float16)
            masks = buf_mask[i:j].to(torch.float16)

            r_sum = rewards.sum().item()
            r_min = rewards.min().item()
            r_max = rewards.max().item()

            self.h_term_buffer.append([states, actions, rewards, masks, r_min, r_max, r_sum])
            i = j

        # throw low r_sum
        q_arg_sort = np.argsort([item[6] for item in self.h_term_buffer])
        h_term_throw = max(0, int(len(self.h_term_buffer) * self.h_term_drop_rate))
        self.h_term_buffer = [self.h_term_buffer[i] for i in q_arg_sort[h_term_throw:]]

        '''update h-term buffer (states, actions, rewards_sum_norm)'''
        self.ten_state = torch.vstack([item[0].to(torch.float32) for item in self.h_term_buffer])
        self.ten_action = torch.vstack([item[1].to(torch.float32) for item in self.h_term_buffer])
        self.ten_mask = torch.vstack([item[3].to(torch.float32) for item in self.h_term_buffer]).squeeze(1)

        r_min = np.min(np.array([item[4] for item in self.h_term_buffer]))
        r_max = np.max(np.array([item[5] for item in self.h_term_buffer]))
        ten_reward = torch.vstack([item[2].to(torch.float32) for item in self.h_term_buffer]).squeeze(1) 
        self.ten_r_norm = (ten_reward - r_min) / (r_max - r_min) 

    def get_obj_h_term_k(self) -> Tensor:
        if self.ten_state is None or self.ten_state.shape[0] < 2 ** 12:
            return torch.zeros(1, dtype=torch.float32, device=self.device)

        '''rd sample'''
        k0 = self.h_term_k_step
        h_term_batch_size = self.batch_size // k0
        indices = torch.randint(k0, self.ten_state.shape[0], size=(h_term_batch_size,),
                                requires_grad=False, device=self.device)

        '''hamilton (K-spin, K=k)'''
        hamilton = torch.zeros((h_term_batch_size,), dtype=torch.float32, device=self.device)
        #print(h_term_batch_size, indices.shape, k0, self.ten_state.shape)
        #print(self.ten_mask[:10])
        #assert 0
        obj_h = torch.zeros((h_term_batch_size, ), dtype=torch.float32, device=self.device)
        discount = 1.0
        for k1 in range(k0 - 1, -1, -1):
            indices_k = indices - k1

            ten_state = self.ten_state[indices_k]
            ten_action = self.ten_action[indices_k]
            ten_mask = self.ten_mask[indices_k]
            discount *= self.h_term_gamma
            logprob = self.act.get_logprob(ten_state, ten_action)
            hamilton = logprob.sum(dim=1) + hamilton
            obj_h += hamilton.clamp(-16,2) * self.ten_r_norm[indices_k] * discount
        return -obj_h.mean() * self.h_term_lambda



class Tracker:
    def __init__(self, max_len):
        self.moving_average = deque([0 for _ in range(max_len)], maxlen=max_len)
        self.max_len = max_len

    def __repr__(self):
        return self.moving_average.__repr__()

    def update(self, values):
        self.moving_average.extend(values.tolist())

    def mean(self):
        return sum(self.moving_average) / self.max_len
