from __future__ import division
from __future__ import print_function

import os
import time
import tensorflow as tf
import numpy as np
import sklearn
from sklearn import metrics

from graphsage.nas_supervised_models import NASSupervisedGraphsage
from graphsage.models import SAGEInfo
from graphsage.minibatch import NodeMinibatchIterator
from graphsage.neigh_samplers import UniformNeighborSampler
from graphsage.utils import load_data,get_rewards

os.environ["CUDA_DEVICE_ORDER"]="PCI_BUS_ID"



# Settings
flags = tf.app.flags
FLAGS = flags.FLAGS

tf.app.flags.DEFINE_boolean('log_device_placement', False,
                            """Whether to log device placement.""")
#core params..
flags.DEFINE_string('model', 'graphsage_mean', 'model names. See README for possible values.')
flags.DEFINE_float('learning_rate', 0.001, 'initial learning rate.')
flags.DEFINE_string("model_size", "small", "Can be big or small; model specific def'ns")
# flags.DEFINE_string('train_prefix', '../example_data/ppi', 'prefix identifying training data. must be specified.')
flags.DEFINE_string('train_prefix', '/home/gaoyang/PycharmProject/ProcessDataset/cora', 'prefix identifying training data. must be specified.')

# left to default values in main experiments
flags.DEFINE_integer('epochs', 10, 'number of epochs to train.')
flags.DEFINE_float('dropout', 0.0, 'dropout rate (1 - keep probability).')
flags.DEFINE_float('weight_decay', 0.0, 'weight for l2 loss on embedding matrix.')
flags.DEFINE_integer('max_degree', 128, 'maximum node degree.')
flags.DEFINE_integer('samples_1', 25, 'number of samples in layer 1')
flags.DEFINE_integer('samples_2', 10, 'number of samples in layer 2')
flags.DEFINE_integer('samples_3', 0, 'number of users samples in layer 3. (Only for mean model)')
flags.DEFINE_integer('dim_1', 128, 'Size of output dim (final is 2x this, if using concat)')
flags.DEFINE_integer('dim_2', 128, 'Size of output dim (final is 2x this, if using concat)')
flags.DEFINE_boolean('random_context', True, 'Whether to use random context or direct edges')
flags.DEFINE_integer('batch_size', 512, 'minibatch size.')
flags.DEFINE_boolean('sigmoid', False, 'whether to use sigmoid loss')
flags.DEFINE_integer('identity_dim', 0, 'Set to positive value to use identity embedding features of that dimension. Default 0.')

#logging, saving, validation settings etc.
flags.DEFINE_string('base_log_dir', '.', 'base directory for logging and saving embeddings')
flags.DEFINE_integer('validate_iter', 50, "how often to run a validation minibatch.")
flags.DEFINE_integer('validate_batch_size', 256, "how many nodes per validation sample.")
flags.DEFINE_integer('gpu', 3, "which gpu to use.")
flags.DEFINE_integer('print_every', 5, "How often to print training info.")
flags.DEFINE_integer('max_total_steps', 10**3, "Maximum total number of iterations")

os.environ["CUDA_VISIBLE_DEVICES"]=str(FLAGS.gpu)

GPU_MEM_FRACTION = 0.8

def calc_f1(y_true, y_pred):
    if not FLAGS.sigmoid:
        y_true = np.argmax(y_true, axis=1)
        y_pred = np.argmax(y_pred, axis=1)
    else:
        y_pred[y_pred > 0.5] = 1
        y_pred[y_pred <= 0.5] = 0
    return metrics.f1_score(y_true, y_pred, average="micro"), metrics.f1_score(y_true, y_pred, average="macro")

# Define model evaluation function
def evaluate(sess, model, minibatch_iter, size=None):
    t_test = time.time()
    feed_dict_val, labels = minibatch_iter.node_val_feed_dict(size)
    node_outs_val = sess.run([model.preds, model.loss],
                        feed_dict=feed_dict_val)
    mic, mac = calc_f1(labels, node_outs_val[0])
    return node_outs_val[1], mic, mac, (time.time() - t_test)

def log_dir(actions):
    log_dir = FLAGS.base_log_dir + "/sup-" + FLAGS.train_prefix.split("/")[-2]
    log_dir += "/{model:s}_{model_size:s}_{lr:0.4f}_{action:s}/".format(
            model=FLAGS.model,
            model_size=FLAGS.model_size,
            lr=FLAGS.learning_rate,
            action=str(actions))
    if not os.path.exists(log_dir):
        os.makedirs(log_dir)
    return log_dir

def incremental_evaluate(sess, model, minibatch_iter, size, test=False):
    t_test = time.time()
    finished = False
    val_losses = []
    val_preds = []
    labels = []
    iter_num = 0
    finished = False
    while not finished:
        feed_dict_val, batch_labels, finished, _  = minibatch_iter.incremental_node_val_feed_dict(size, iter_num, test=test)
        node_outs_val = sess.run([model.preds, model.loss],
                         feed_dict=feed_dict_val)
        val_preds.append(node_outs_val[0])
        labels.append(batch_labels)
        val_losses.append(node_outs_val[1])
        iter_num += 1
    val_preds = np.vstack(val_preds)
    labels = np.vstack(labels)
    f1_scores = calc_f1(labels, val_preds)
    return np.mean(val_losses), f1_scores[0], f1_scores[1], (time.time() - t_test)

def construct_placeholders(num_classes):
    # Define placeholders
    placeholders = {
        'labels' : tf.placeholder(tf.float32, shape=(None, num_classes), name='labels'),
        'batch' : tf.placeholder(tf.int32, shape=(None), name='batch1'),
        'dropout': tf.placeholder_with_default(0., shape=(), name='dropout'),
        'batch_size' : tf.placeholder(tf.int32, name='batch_size'),
    }
    return placeholders

def train(train_data,action, test_data=None):
    config = tf.ConfigProto(log_device_placement=FLAGS.log_device_placement)
    config.gpu_options.allow_growth = True
    # config.gpu_options.per_process_gpu_memory_fraction = GPU_MEM_FRACTION
    config.allow_soft_placement = True
    # Initialize session
    # 定义作用域，不然会与Controller发生冲突
    with tf.Session(config=config, graph=tf.Graph()) as sess:
        with sess.as_default():
            with sess.graph.as_default():
                # Set random seed
                seed = 123
                np.random.seed(seed)
                tf.set_random_seed(seed)

                G = train_data[0] # 图数据
                features = train_data[1] #节点特征值
                id_map = train_data[2] #节点id对index的映射
                class_map  = train_data[4] # 节点类别
                #计算类别数量
                if isinstance(list(class_map.values())[0], list):
                    num_classes = len(list(class_map.values())[0])
                else:
                    num_classes = len(set(class_map.values()))
                # 添加一个全0的数据，不知道用途
                if not features is None:
                    # pad with dummy zero vector
                    features = np.vstack([features, np.zeros((features.shape[1],))])
                # 随机游走生成共现边，用来替换图中边信息
                context_pairs = train_data[3] if FLAGS.random_context else None
                placeholders = construct_placeholders(num_classes)
                minibatch = NodeMinibatchIterator(G,
                        id_map,
                        placeholders,
                        class_map,
                        num_classes,
                        batch_size=FLAGS.batch_size,
                        max_degree=FLAGS.max_degree,
                        context_pairs = context_pairs)# 用于批处理
                adj_info_ph = tf.placeholder(tf.int32, shape=minibatch.adj.shape)
                adj_info = tf.Variable(adj_info_ph, trainable=False, name="adj_info")
                # 创建模型
                sampler = UniformNeighborSampler(adj_info)# 邻居采样，方式为随机重排邻居
                state_nums = 2 # Controller定义的状态数量
                layers_num = len(action) // state_nums #计算层数
                layer_infos = []
                # 用于指导最终GNN的生层，这里只修改了采样数量
                # for i in range(layers_num):
                layer_infos.append(SAGEInfo("node", sampler, FLAGS.samples_1, FLAGS.dim_1))
                layer_infos.append(SAGEInfo("node", sampler, FLAGS.samples_2, FLAGS.dim_1))
                # 用于NAS的监督GraphSage
                model = NASSupervisedGraphsage(num_classes, placeholders,
                                            features,
                                            adj_info,
                                            minibatch.deg,
                                            layer_infos,
                                            state_nums = state_nums,
                                            action= action,
                                            model_size=FLAGS.model_size,
                                            sigmoid_loss=FLAGS.sigmoid,
                                            identity_dim=FLAGS.identity_dim,
                                            logging=True)

                # 记录
                merged = tf.summary.merge_all()
                summary_writer = tf.summary.FileWriter(log_dir(action), sess.graph)

                # Init variables
                sess.run(tf.global_variables_initializer(), feed_dict={adj_info_ph: minibatch.adj})

                # Train model

                total_steps = 0
                avg_time = 0.0
                epoch_val_costs = []

                train_adj_info = tf.assign(adj_info, minibatch.adj)
                val_adj_info = tf.assign(adj_info, minibatch.test_adj)
                for epoch in range(FLAGS.epochs):
                    minibatch.shuffle()

                    iter = 0
                    print('Epoch: %04d' % (epoch + 1))
                    epoch_val_costs.append(0)
                    while not minibatch.end():
                        # Construct feed dictionary
                        feed_dict, labels = minibatch.next_minibatch_feed_dict()
                        feed_dict.update({placeholders['dropout']: FLAGS.dropout})

                        t = time.time()
                        # Training step
                        outs = sess.run([merged, model.opt_op, model.loss, model.preds], feed_dict=feed_dict)
                        train_cost = outs[2]

                        if iter % FLAGS.validate_iter == 0:
                            # Validation
                            sess.run(val_adj_info.op)
                            if FLAGS.validate_batch_size == -1:
                                val_cost, val_f1_mic, val_f1_mac, duration = incremental_evaluate(sess, model, minibatch, FLAGS.batch_size)
                            else:
                                val_cost, val_f1_mic, val_f1_mac, duration = evaluate(sess, model, minibatch, FLAGS.validate_batch_size)
                            sess.run(train_adj_info.op)
                            epoch_val_costs[-1] += val_cost

                        if total_steps % FLAGS.print_every == 0:
                            summary_writer.add_summary(outs[0], total_steps)

                        # Print results
                        avg_time = (avg_time * total_steps + time.time() - t) / (total_steps + 1)

                        if total_steps % FLAGS.print_every == 0:
                            train_f1_mic, train_f1_mac = calc_f1(labels, outs[-1])
                            print("Iter:", '%04d' % iter,
                                  "train_loss=", "{:.5f}".format(train_cost),
                                  "train_f1_mic=", "{:.5f}".format(train_f1_mic),
                                  "train_f1_mac=", "{:.5f}".format(train_f1_mac),
                                  "val_loss=", "{:.5f}".format(val_cost),
                                  "val_f1_mic=", "{:.5f}".format(val_f1_mic),
                                  "val_f1_mac=", "{:.5f}".format(val_f1_mac),
                                  "time=", "{:.5f}".format(avg_time))

                        iter += 1
                        total_steps += 1

                        if total_steps > FLAGS.max_total_steps:
                            break

                    if total_steps > FLAGS.max_total_steps:
                            break

                print("Optimization Finished!")
                sess.run(val_adj_info.op)
                # 分批（增量式）验证
                val_cost, val_f1_mic, val_f1_mac, duration = incremental_evaluate(sess, model, minibatch, FLAGS.batch_size)
                print("Full validation stats:",
                              "loss=", "{:.5f}".format(val_cost),
                              "f1_micro=", "{:.5f}".format(val_f1_mic),
                              "f1_macro=", "{:.5f}".format(val_f1_mac),
                              "time=", "{:.5f}".format(duration))
                with open(log_dir(action) + "val_stats.txt", "w") as fp:
                    fp.write("loss={:.5f} f1_micro={:.5f} f1_macro={:.5f} time={:.5f}".
                            format(val_cost, val_f1_mic, val_f1_mac, duration))

                print("Writing test set stats to file (don't peak!)")
                val_cost, val_f1_mic, val_f1_mac, duration = incremental_evaluate(sess, model, minibatch, FLAGS.batch_size, test=True)
                with open(log_dir(action) + "test_stats.txt", "w") as fp:
                    fp.write("loss={:.5f} f1_micro={:.5f} f1_macro={:.5f}".
                            format(val_cost, val_f1_mic, val_f1_mac))
    tf.reset_default_graph()
    #用f1指数替换accuracy，此处未做滑动指数平均
    return get_rewards(val_f1_mic),val_f1_mic
    # return (1,1)

moving_acc = 0
train_data = None
structure = [1, 'relu',1, 'linear']
structure3 = [0, 'relu',3, 'relu']
structure4 = [1, 'tanh',0, 'sigmoid']
[1, 'tanh', 0, 'linear']
def main(argv=None,action=[4, 'sigmoid', 1, 'tanh']):
    global train_data
    print(action)
    # 共享训练数据
    if train_data == None:
        print("Loading training data..")
        train_data = load_data(FLAGS.train_prefix)
        print("Done loading training data..")
    for _ in range(1):
        train(train_data,action)

if __name__ == '__main__':
    tf.app.run()
