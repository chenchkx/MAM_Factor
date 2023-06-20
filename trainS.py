#!/home/lw/miniconda3/envs/RL/bin/python
# a common file to run all exps
import sys
import numpy as np
import pandapower.networks

# print('Python %s on %s' % (sys.version, sys.platform))
# sys.path.extend(['/home/lw/RL_Qlearning', '/home/lw/RL_Qlearning'])
import os
# os.environ['CUDA_VISIBLE_DEVICES'] = "1"
import pprint
import tianshou as ts
import torch
import argparse
from tianshou.utils.net.common import Net, ActorCritic
from tianshou.policy import DQNPolicy, PPOPolicy, A2CPolicy
from tianshou.data import VectorReplayBuffer
from collector import Collector, AsyncCollector
from utils import NewLogger as TensorboardLogger
from offpolicy import offpolicy_trainer
from onpolicy import onpolicy_trainer
from single_transmission_graph_section import TransmissionSectionEnv as singleEnv  # single env for S4,S10 task
from networks import SoftNet, MLPBase, SelfAttentionNetWeighted,SelfAttentionNetWeighted_KAIXUAN
from tianshou.utils.net.discrete import Actor, Critic
from torch.utils.tensorboard import SummaryWriter


def get_args():
    parser = argparse.ArgumentParser()
    parser.add_argument('--case', type=str, default='case118')  # case118, case300, case9241
    parser.add_argument('--task', type=str, default='S4')  # S4, S10
    parser.add_argument('--method', type=str, default='MAM')  # DQN, doubleDQN, duelingDQN, PPO, MAM, A2C
    parser.add_argument('--model', type=str, default='Attention')  # Soft, Concat, Attention
    parser.add_argument('--order', type=int, default=1)

    # S4case118, S4case300, S10case118, S10case300, S10case9241
    parser.add_argument('--env_id', type=str, default='None')
    # int for random sections, list for fixes sections, e.g. 5 or [1, 2, 3, 7, 9]
    parser.add_argument('--task_id', default=3)

    parser.add_argument('--train_env_num', type=int, default=10)  # 10
    parser.add_argument('--test_env_num', type=int, default=1)
    parser.add_argument('--reward_threshold', type=float, default=99)
    parser.add_argument('--render', type=float, default=0.)
    parser.add_argument('--eps_train_high', type=float, default=1)
    parser.add_argument('--eps_train_low', type=float, default=0.05)
    parser.add_argument('--eps_train', type=float, default=0.1)
    parser.add_argument('--eps_test', type=float, default=0)
    parser.add_argument('--lr', type=float, default=0.001)
    parser.add_argument('--max_epoch', type=int, default=25)
    parser.add_argument('--step_per_epoch', type=int, default=40000)  # 2000
    parser.add_argument('--step_per_collect', type=int, default=50)  # 50
    parser.add_argument('--capacity', type=int, default=20000)
    # parser.add_argument('--batch_size', type=int, default=64)
    parser.add_argument('--batch_size', type=int, default=64)
    parser.add_argument('--gamma', type=float, default=0.9)
    parser.add_argument('--est_step', type=int, default=3)  # TD(lambda)
    parser.add_argument('--episode_per_test', type=int, default=None)  # according to sample number of test env
    parser.add_argument('--update_per_step', type=float, default=0.1)
    parser.add_argument('--logdir', type=str, default='log')
    parser.add_argument('--device', type=str, default='cuda' if torch.cuda.is_available() else 'cpu')
    parser.add_argument('--dueling_param', default=None)
    parser.add_argument('--is_double', default=True)
    parser.add_argument(
        # '--hidden_size', type=int, nargs='*', default=[128, 128]
        '--hidden_size', type=int, nargs='*', default=[128]
    )
    # ppo special
    parser.add_argument('--vf-coef', type=float, default=0.5)
    parser.add_argument('--ent-coef', type=float, default=0.0)
    parser.add_argument('--eps-clip', type=float, default=0.2)
    parser.add_argument('--max-grad-norm', type=float, default=0.5)
    parser.add_argument('--gae-lambda', type=float, default=0.95)
    parser.add_argument('--rew-norm', type=int, default=1)
    parser.add_argument('--norm-adv', type=int, default=1)
    parser.add_argument('--recompute-adv', type=int, default=0)
    parser.add_argument('--dual-clip', type=float, default=None)
    parser.add_argument('--value-clip', type=int, default=1)
    parser.add_argument('--resume-path', type=str, default=None)
    parser.add_argument('--repeat-per-collect', type=int, default=2)

    parser.add_argument('--hidden_type', type=str, default='GINE_Factor')

    args = parser.parse_known_args()[0]
    return args


def dqn(args=get_args()):
    if args.order == 0:
        assert False, 'Check the order'

    if args.task == 'S10' or args.task == 'S4':
        actEnv = singleEnv
        args.task_id = 1
    else:
        assert False, 'task not in S4, S10'

    args.env_id = args.task + args.case
    print(args.env_id+'/'+args.method+'/'+args.model+'_'+str(args.order)+'_'+args.hidden_type)
    env = actEnv(args, evaluation=True)
    args.state_dim = env.observation_space.shape[0] or env.observation_space.n
    args.action_dim = env.action_space.shape or env.action_space.n
    print(args.state_dim, args.action_dim, env.n_net)
    # g = env.graph
    args.episode_per_test = env.n_net

    train_env = ts.env.SubprocVectorEnv([lambda: actEnv(args) for _ in range(args.train_env_num)], wait_num=5, timeout=0.2)  # 并行采样
    test_env = ts.env.DummyVectorEnv(
        [lambda: actEnv(args, evaluation=True) for _ in range(args.test_env_num)])
    task_num = None
    if isinstance(args.task_id, int):
        task_num = args.task_id
    elif isinstance(args.task_id, list):
        task_num = len(args.task_id)

    # pack method&model to policy
    if args.method == 'PPO' or args.method == 'A2C':
        if args.model == 'Soft':
            net = SoftNet(
                output_shape=args.action_dim,
                base_type=MLPBase,
                em_input_shape=task_num * env.n_line,
                input_shape=args.state_dim - task_num * env.n_line,
                em_hidden_shapes=[args.hidden_size[0]],
                hidden_shapes=args.hidden_size,
                num_layers=2,
                num_modules=2,
                module_hidden=args.hidden_size[0],
                gating_hidden=args.hidden_size[0],
                num_gating_layers=2,
                add_bn=False,
                pre_softmax=False,
                dueling_param=None,
                is_last=False,
                softmax=True
            ).to(args.device)
            actor = Actor(net, args.action_dim, device=args.device,
                          preprocess_net_output_dim=args.hidden_size[0]).to(args.device)
            critic = Critic(net, device=args.device, preprocess_net_output_dim=args.hidden_size[0]).to(args.device)
        elif args.model == 'Concat':
            net = Net(args.state_dim, hidden_sizes=args.hidden_size,
                      device=args.device, dueling_param=None, softmax=True).to(args.device)
            actor = Actor(net, args.action_dim, device=args.device).to(args.device)
            critic = Critic(net, device=args.device).to(args.device)
        else:
            assert False, 'PPO method can only be applied on Soft or Concat model'
        actor_critic = ActorCritic(actor, critic)
        # orthogonal initialization
        for m in actor_critic.modules():
            if isinstance(m, torch.nn.Linear):
                torch.nn.init.orthogonal_(m.weight)
                torch.nn.init.zeros_(m.bias)
        optim = torch.optim.Adam(actor_critic.parameters(), lr=args.lr)
        dist = torch.distributions.Categorical
        if args.method == 'PPO':
            policy = PPOPolicy(
                actor,
                critic,
                optim,
                dist,
                discount_factor=args.gamma,
                max_grad_norm=args.max_grad_norm,
                eps_clip=args.eps_clip,
                vf_coef=args.vf_coef,
                ent_coef=args.ent_coef,
                gae_lambda=args.gae_lambda,
                reward_normalization=args.rew_norm,
                dual_clip=args.dual_clip,
                value_clip=args.value_clip,
                action_space=env.action_space,
                deterministic_eval=True,
                advantage_normalization=args.norm_adv,
                recompute_advantage=args.recompute_adv
            )
        elif args.method == 'A2C':
            policy = A2CPolicy(
                actor,
                critic,
                optim,
                dist,
                discount_factor=args.gamma,
                gae_lambda=args.gae_lambda,
                vf_coef=args.vf_coef,
                ent_coef=args.ent_coef,
                max_grad_norm=args.max_grad_norm,
                reward_normalization=args.rew_norm,
                action_space=env.action_space
            )
        else:
            assert False, 'On-policy method either in PPO or A2C'

    else:
        Q_param = V_param = {"hidden_sizes": [128]}
        if args.method == 'duelingDQN' or args.method == 'MAM':
            args.dueling_param = (Q_param, V_param)
        elif args.method == 'DQN':
            args.is_double = False

        if args.method == 'MAM':
            args.model = 'Attention'
            # net = SelfAttentionNetWeighted(output_shape=args.action_dim,
            #                          em_input_shape=env.n_line,
            #                          state_input_shape=args.state_dim - task_num * env.n_line,
            #                          task_num=task_num,
            #                          graph_u=env.graph_u,
            #                          graph_d=env.graph_d,
            #                          hidden_type=args.hidden_type,
            #                          dueling_param=args.dueling_param,
            #                          device='cuda',
            #                          ).to(args.device)
            net = SelfAttentionNetWeighted_KAIXUAN(output_shape=args.action_dim,
                                     em_input_shape=env.n_line,
                                     state_input_shape=args.state_dim - task_num * env.n_line,
                                     task_num=task_num,
                                     graph_u=env.graph_u,
                                     graph_d=env.graph_d,
                                     hidden_type=args.hidden_type,
                                     dueling_param=args.dueling_param,
                                     device='cuda',
                                     ).to(args.device)
        elif args.model == 'Soft':
            net = SoftNet(output_shape=args.action_dim,
                          base_type=MLPBase,
                          em_input_shape=task_num * env.n_line,
                          input_shape=args.state_dim - task_num * env.n_line,
                          em_hidden_shapes=[128],
                          hidden_shapes=args.hidden_size,
                          num_layers=2,
                          num_modules=2,
                          module_hidden=256,
                          gating_hidden=256,
                          num_gating_layers=2,
                          dueling_param=args.dueling_param,
                          ).to(args.device)

        elif args.model == 'Concat':
            net = Net(
                args.state_dim,
                args.action_dim,
                hidden_sizes=args.hidden_size,
                device=args.device,
                dueling_param=args.dueling_param,
            ).to(args.device)
        else:
            assert False, 'Invalid model type!'

        optim = torch.optim.Adam(net.parameters(), lr=args.lr)

        policy = DQNPolicy(
            net,
            optim,
            args.gamma,
            args.est_step,  # 向前看几步
            target_update_freq=100,  # target_network的参数更新频率
            is_double=args.is_double
        )
    log_order = '_'+str(args.order)
    info = ''

    # args.resume_path = os.path.join(args.logdir, args.env_id, args.method+args.model+info+log_order, 'policy.pth')
    resume_from_path = False
    if args.resume_path:
        policy.load_state_dict(torch.load(args.resume_path, map_location=args.device))
        resume_from_path = True
        print("Loaded agent from: ", args.resume_path)

    # replay
    buf = VectorReplayBuffer(args.capacity, buffer_num=len(train_env))
    # collector
    train_collector = AsyncCollector(policy, train_env, buf, exploration_noise=True)
    # train_collector = Collector(policy, train_env, buf, exploration_noise=True)
    test_collector = Collector(policy, test_env)

    # train logger
    log_path = os.path.join(args.logdir, args.env_id, args.method + args.model + info + log_order + args.hidden_type)
    writer = SummaryWriter(log_path)
    logger = TensorboardLogger(writer, train_interval=1, update_interval=1)
    # para logger
    argsDict = vars(args)
    para_path = os.path.join(args.logdir, args.env_id, args.method + args.model + info + log_order + args.hidden_type, 'params.txt')
    with open(para_path, 'w') as f:
        f.writelines('------------------ start ------------------' + '\n')
        for eachArg, value in argsDict.items():
            f.writelines(eachArg + ' : ' + str(value) + '\n')
        f.writelines('------------------- end -------------------')

    def save_fn(save_policy):
        torch.save(save_policy.state_dict(), os.path.join(log_path, 'policy.pth'))

    # stop function
    def stop_fn(mean_rewards):
        return mean_rewards >= args.reward_threshold

    # # # set epsilon
    # def train_fn(epoch, env_step):
    #     if env_step < 2e5:
    #         eps = args.eps_train_high - int(env_step/20000) * 0.1
    #         policy.set_eps(eps)
    #     else:
    #         policy.set_eps(args.eps_train_low)

    def train_fn(epoch, env_step):
        if env_step <= (args.step_per_epoch * args.max_epoch) / 5:
            policy.set_eps(args.eps_train)
        elif env_step <= (args.step_per_epoch * args.max_epoch) / 2:
            eps = 0.5 * args.eps_train
            policy.set_eps(eps)
        else:
            policy.set_eps(0.1 * args.eps_train)

    # 测试前调用,设定eps
    def test_fn(epoch, env_step):
        policy.set_eps(args.eps_test)

    def save_checkpoint_fn(epoch, env_step, gradient_step):
        ckpt_path = os.path.join(log_path, 'checkpoint.pth')
        torch.save({'model': policy.state_dict()}, ckpt_path)
        return ckpt_path

    if args.method == 'PPO' or args.method == 'A2C':
        result = onpolicy_trainer(
            policy,
            train_collector,
            test_collector,
            args.max_epoch,
            args.step_per_epoch,
            args.repeat_per_collect,
            episode_per_test=args.episode_per_test,
            batch_size=args.batch_size,
            step_per_collect=args.step_per_collect,
            stop_fn=stop_fn,
            save_fn=save_fn,
            logger=logger,
            resume_from_log=resume_from_path,
            save_checkpoint_fn=save_checkpoint_fn,
            test_in_train=False
        )
    else:
        # 采样
        train_collector.collect(n_step=args.batch_size * args.train_env_num, render=args.render)
        result = offpolicy_trainer(
            policy,
            train_collector=train_collector,
            test_collector=test_collector,
            max_epoch=args.max_epoch,
            step_per_epoch=args.step_per_epoch,
            step_per_collect=args.step_per_collect,
            episode_per_test=args.episode_per_test,
            batch_size=args.batch_size,
            update_per_step=args.update_per_step,
            save_checkpoint_fn=save_checkpoint_fn,
            resume_from_log=resume_from_path,
            train_fn=train_fn,
            test_fn=test_fn,
            stop_fn=stop_fn,
            save_fn=save_fn,
            logger=logger,
            test_in_train=False
        )

    if __name__ == '__main__':
        pprint.pprint(result)
        print('is running')
        env = ts.env.DummyVectorEnv([lambda: actEnv(args, evaluation=True)
                                     for _ in range(args.train_env_num)])
        policy.eval()
        if args.method != 'PPO' and args.method != 'A2C':
            policy.set_eps(args.eps_test)
        collector = Collector(policy, env)
        result = collector.collect(n_episode=100, render=args.render)
        rews, lens = result["rews"], result["lens"]
        print(f"Final reward: {rews.mean()}, length: {lens.mean()}")
        print(args.env_id+'/'+args.method+'/'+args.model+'_'+str(args.order))


if __name__ == '__main__':
    dqn(get_args())
