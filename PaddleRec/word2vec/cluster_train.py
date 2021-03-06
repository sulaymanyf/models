from __future__ import print_function
import argparse
import logging
import os
import time
import math
import random
import numpy as np
import paddle
import paddle.fluid as fluid
import six
import reader
from net import skip_gram_word2vec

logging.basicConfig(format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger("fluid")
logger.setLevel(logging.INFO)


def parse_args():
    parser = argparse.ArgumentParser(
        description="PaddlePaddle Word2vec example")
    parser.add_argument(
        '--train_data_dir',
        type=str,
        default='./data/text',
        help="The path of taining dataset")
    parser.add_argument(
        '--base_lr',
        type=float,
        default=0.01,
        help="The number of learing rate (default: 0.01)")
    parser.add_argument(
        '--save_step',
        type=int,
        default=500000,
        help="The number of step to save (default: 500000)")
    parser.add_argument(
        '--print_batch',
        type=int,
        default=100,
        help="The number of print_batch (default: 10)")
    parser.add_argument(
        '--dict_path',
        type=str,
        default='./data/1-billion_dict',
        help="The path of data dict")
    parser.add_argument(
        '--batch_size',
        type=int,
        default=500,
        help="The size of mini-batch (default:500)")
    parser.add_argument(
        '--num_passes',
        type=int,
        default=10,
        help="The number of passes to train (default: 10)")
    parser.add_argument(
        '--model_output_dir',
        type=str,
        default='models',
        help='The path for model to store (default: models)')
    parser.add_argument('--nce_num', type=int, default=5, help='nce_num')
    parser.add_argument(
        '--embedding_size',
        type=int,
        default=64,
        help='sparse feature hashing space for index processing')
    parser.add_argument(
        '--is_sparse',
        action='store_true',
        required=False,
        default=False,
        help='embedding and nce will use sparse or not, (default: False)')
    parser.add_argument(
        '--with_speed',
        action='store_true',
        required=False,
        default=False,
        help='print speed or not , (default: False)')
    parser.add_argument(
        '--role', type=str, default='pserver', help='trainer or pserver')
    parser.add_argument(
        '--endpoints',
        type=str,
        default='127.0.0.1:6000',
        help='The pserver endpoints, like: 127.0.0.1:6000, 127.0.0.1:6001')
    parser.add_argument(
        '--current_endpoint',
        type=str,
        default='127.0.0.1:6000',
        help='The current_endpoint')
    parser.add_argument(
        '--trainer_id',
        type=int,
        default=0,
        help='trainer id ,only trainer_id=0 save model')
    parser.add_argument(
        '--trainers',
        type=int,
        default=1,
        help='The num of trianers, (default: 1)')
    return parser.parse_args()


def convert_python_to_tensor(weight, batch_size, sample_reader):
    def __reader__():
        cs = np.array(weight).cumsum()
        result = [[], []]
        for sample in sample_reader():
            for i, fea in enumerate(sample):
                result[i].append(fea)
            if len(result[0]) == batch_size:
                tensor_result = []
                for tensor in result:
                    t = fluid.Tensor()
                    dat = np.array(tensor, dtype='int64')
                    if len(dat.shape) > 2:
                        dat = dat.reshape((dat.shape[0], dat.shape[2]))
                    elif len(dat.shape) == 1:
                        dat = dat.reshape((-1, 1))
                    t.set(dat, fluid.CPUPlace())
                    tensor_result.append(t)
                tt = fluid.Tensor()
                neg_array = cs.searchsorted(np.random.sample(args.nce_num))
                neg_array = np.tile(neg_array, batch_size)
                tt.set(
                    neg_array.reshape((batch_size, args.nce_num)),
                    fluid.CPUPlace())
                tensor_result.append(tt)
                yield tensor_result
                result = [[], []]

    return __reader__


def train_loop(args, train_program, reader, data_loader, loss, trainer_id,
               weight):

    data_loader.set_batch_generator(
        convert_python_to_tensor(weight, args.batch_size, reader.train()))

    place = fluid.CPUPlace()
    exe = fluid.Executor(place)
    exe.run(fluid.default_startup_program())

    print("CPU_NUM:" + str(os.getenv("CPU_NUM")))

    train_exe = exe

    for pass_id in range(args.num_passes):
        data_loader.start()
        time.sleep(10)
        epoch_start = time.time()
        batch_id = 0
        start = time.time()
        try:
            while True:

                loss_val = train_exe.run(fetch_list=[loss.name])
                loss_val = np.mean(loss_val)

                if batch_id % args.print_batch == 0:
                    logger.info(
                        "TRAIN --> pass: {} batch: {} loss: {} reader queue:{}".
                        format(pass_id, batch_id,
                               loss_val.mean(), data_loader.queue.size()))
                if args.with_speed:
                    if batch_id % 500 == 0 and batch_id != 0:
                        elapsed = (time.time() - start)
                        start = time.time()
                        samples = 1001 * args.batch_size * int(
                            os.getenv("CPU_NUM"))
                        logger.info("Time used: {}, Samples/Sec: {}".format(
                            elapsed, samples / elapsed))

                if batch_id % args.save_step == 0 and batch_id != 0:
                    model_dir = args.model_output_dir + '/pass-' + str(
                        pass_id) + ('/batch-' + str(batch_id))
                    if trainer_id == 0:
                        fluid.save(fluid.default_main_program(), model_path=model_dir)
                        print("model saved in %s" % model_dir)
                batch_id += 1

        except fluid.core.EOFException:
            data_loader.reset()
            epoch_end = time.time()
            logger.info("Epoch: {0}, Train total expend: {1} ".format(
                pass_id, epoch_end - epoch_start))
            model_dir = args.model_output_dir + '/pass-' + str(pass_id)
            if trainer_id == 0:
                fluid.save(fluid.default_main_program(), model_path=model_dir)
                print("model saved in %s" % model_dir)


def GetFileList(data_path):
    return os.listdir(data_path)


def train(args):

    if not os.path.isdir(args.model_output_dir) and args.trainer_id == 0:
        os.mkdir(args.model_output_dir)

    filelist = GetFileList(args.train_data_dir)
    word2vec_reader = reader.Word2VecReader(args.dict_path, args.train_data_dir,
                                            filelist, 0, 1)

    logger.info("dict_size: {}".format(word2vec_reader.dict_size))
    np_power = np.power(np.array(word2vec_reader.id_frequencys), 0.75)
    id_frequencys_pow = np_power / np_power.sum()

    loss, data_loader = skip_gram_word2vec(
        word2vec_reader.dict_size,
        args.embedding_size,
        is_sparse=args.is_sparse,
        neg_num=args.nce_num)

    optimizer = fluid.optimizer.SGD(
        learning_rate=fluid.layers.exponential_decay(
            learning_rate=args.base_lr,
            decay_steps=100000,
            decay_rate=0.999,
            staircase=True))

    optimizer.minimize(loss)

    logger.info("run dist training")

    t = fluid.DistributeTranspiler()
    t.transpile(
        args.trainer_id, pservers=args.endpoints, trainers=args.trainers)
    if args.role == "pserver":
        print("run psever")
        pserver_prog = t.get_pserver_program(args.current_endpoint)
        pserver_startup = t.get_startup_program(args.current_endpoint,
                                                pserver_prog)
        exe = fluid.Executor(fluid.CPUPlace())
        exe.run(pserver_startup)
        exe.run(pserver_prog)
    elif args.role == "trainer":
        print("run trainer")
        train_loop(args,
                   t.get_trainer_program(), word2vec_reader, data_loader, loss,
                   args.trainer_id, id_frequencys_pow)


if __name__ == '__main__':
    args = parse_args()
    train(args)
