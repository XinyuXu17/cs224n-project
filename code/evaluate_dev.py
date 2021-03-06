from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

import io
import os
import json
import sys
import random
from os.path import join as pjoin

import numpy as np
from six.moves import xrange
import tensorflow as tf

from qa_model import QASystem
from decoder import BiLSTM_Decoder as Decoder
from encoder import BiLSTM_Encoder as Encoder
from util import load_and_preprocess_data, load_embeddings,split_train_dev
from train import FLAGS
from evaluate import exact_match_score, f1_score
import logging

logging.basicConfig(level=logging.INFO)

def get_normalized_train_dir(train_dir):
    """
    Adds symlink to {train_dir} from /tmp/cs224n-squad-train to canonicalize the
    file paths saved in the checkpoint. This allows the model to be reloaded even
    if the location of the checkpoint files has moved, allowing usage with CodaLab.
    This must be done on both train.py and qa_answer.py in order to work.
    """
    global_train_dir = '/tmp/cs224n-squad-train'
    if os.path.exists(global_train_dir):
        os.unlink(global_train_dir)
    if not os.path.exists(train_dir):
        os.makedirs(train_dir)
    os.symlink(os.path.abspath(train_dir), global_train_dir)
    return global_train_dir

def initialize_model(session, model, train_dir):
    ckpt = tf.train.get_checkpoint_state(train_dir)
    if ckpt:
        print("found model")
    else:
        print("cant'find model")
    v2_path = ckpt.model_checkpoint_path + ".index" if ckpt else ""
    if ckpt and (tf.gfile.Exists(ckpt.model_checkpoint_path) or tf.gfile.Exists(v2_path)):
        logging.info("Reading model parameters from %s" % ckpt.model_checkpoint_path)
        model.saver.restore(session, ckpt.model_checkpoint_path)
    else:
        logging.info("Created model with fresh parameters.")
        session.run(tf.global_variables_initializer())
        logging.info('Num params: %d' % sum(v.get_shape().num_elements() for v in tf.trainable_variables()))
    return model

def initialize_vocab(vocab_path):
    if tf.gfile.Exists(vocab_path):
        rev_vocab = []
        with tf.gfile.GFile(vocab_path, mode="rb") as f:
            rev_vocab.extend(f.readlines())
        rev_vocab = [line.strip('\n') for line in rev_vocab]
        vocab = dict([(x, y) for (y, x) in enumerate(rev_vocab)])
        return vocab, rev_vocab
    else:
        raise ValueError("Vocabulary file %s not found.", vocab_path)

def formulate_answer(context, rev_vocab, start, end, mask = None):
    answer = ''
    for i in range(start, end + 1):
        if i < len(context):
            if mask is None:
                answer +=  rev_vocab[context[i]]
                answer += ' '
            else:
                if mask[i]:
                    answer +=  rev_vocab[context[i]]
                    answer += ' '
    return answer

def construct_result(index, f1, RoW, dataset, true_s, true_e, pre_s, pre_e, pre_ans, true_ans, rev_vocab, relevence_str):
    context = dataset[0]
    context_mask = dataset[1]
    question = dataset[2]
    question_mask = dataset[3]
    machine_list = [index, f1, RoW, sum(context_mask), sum(question_mask), abs(true_s - true_e), true_s, true_e]
    list_str = ' '.join(str(value) for value in machine_list)
    question_string = formulate_answer(question, rev_vocab, 0, len(question) - 1, mask = question_mask)
    context_string = formulate_answer(context, rev_vocab, 0, len(context) - 1, mask = context_mask)
    human_dic = {
        'context': context_string,
        'question': question_string,
        'true_ans': true_ans,
        'predict_ans': pre_ans,
        'relevence': relevence_str
    }
    return list_str, human_dic

def generate_answers(sess, model, dataset, rev_vocab):
    """
    Loop over the dev or test dataset and generate answer.

    :param sess: active TF session
    :param model: a built QASystem model
    :param rev_vocab: this is a list of vocabulary that maps index to actual words
    :return:
    """
    overall_f1 = 0.
    overall_em = 0.
    index = 1
    output_dict = {} # for human
    output_list = list() # for machine
    minibatch_size = 100
    num_batches = int(len(dataset) / minibatch_size)
    average_loss = model.test(sess, dataset)
    print('average loss {}'.format(average_loss))
    for batch in range(0, num_batches):
        start = batch * minibatch_size
        print("batch {} out of {}".format(batch+1, num_batches))
        batch_f1 = 0.
        batch_em = 0.
        h_s, h_e, relevence = model.decode(sess, dataset[start:start + minibatch_size])
        for i in range(minibatch_size):
            a_s = np.argmax(h_s[i])
            a_e = np.argmax(h_e[i])
            if a_s > a_e:
                k = a_e
                a_e = a_s
                a_s = k

            relevence_str = ' '.join(str(value) for value in relevence[i])
            sample_dataset = dataset[start + i]
            context = sample_dataset[0]
            (a_s_true, a_e_true) = sample_dataset[6]
            predicted_answer = model.formulate_answer(context, rev_vocab, a_s, a_e)
            true_answer = model.formulate_answer(context, rev_vocab, a_s_true, a_e_true)
            f1 = f1_score(predicted_answer, true_answer)
            overall_f1 += f1
            batch_f1 += f1

            if exact_match_score(predicted_answer, true_answer):
                overall_em += 1
                batch_em += 1
                RoW = 1
            else:
                RoW = 0

            # output result
            tmp_list, tmp_dict = construct_result(
                index, f1, RoW, sample_dataset,
                a_s_true, a_e_true,
                a_s, a_e,
                predicted_answer, true_answer,
                rev_vocab, relevence_str
            )
            output_list.append(tmp_list)
            output_dict[index] = tmp_dict
            index += 1

        print("batch F1: {}".format(batch_f1/minibatch_size))
        print("batch EM: {}".format(batch_em/minibatch_size))
    print("overall F1: {}".format(overall_f1/(num_batches*minibatch_size)))
    print("overall EM: {}".format(overall_em/(num_batches*minibatch_size)))
    print("overall val loss: {}".format(average_loss))
    return output_list, output_dict

def store_result(output_list, output_dict, train_dir):
    machine_file_dir = os.path.join(train_dir, 'validation_predict.txt')
    human_file_dir = os.path.join(train_dir, 'validation_predict.json')
    # dump machine read file
    with open(machine_file_dir, 'w') as outfile:
        outfile.write("\n".join(output_list))
    with open(human_file_dir, 'w') as outfile:
        json.dump(output_dict, outfile)

def main(_):
    #======Fill the model name=============
    train_dir = "train/test"
    #======================================
    vocab, rev_vocab = initialize_vocab(FLAGS.vocab_path)
    embed_path = FLAGS.embed_path or pjoin("data", "squad", "glove.trimmed.{}.npz".format(FLAGS.embedding_size))

    if not os.path.exists(FLAGS.log_dir):
        os.makedirs(FLAGS.log_dir)
    file_handler = logging.FileHandler(pjoin(FLAGS.log_dir, "log.txt"))
    logging.getLogger().addHandler(file_handler)

    print(vars(FLAGS))

    # ========= Load Dataset =========
    train_data,val_data  = load_and_preprocess_data(FLAGS.data_dir, FLAGS.max_context_len, FLAGS.max_question_len, size = FLAGS.train_size)
    # ========= Model-specific =========
    # You must change the following code to adjust to your model

    embed_path = FLAGS.embed_path or pjoin("data", "squad", "glove.trimmed.{}.npz".format(FLAGS.embedding_size))
    vocab_path = FLAGS.vocab_path or pjoin(FLAGS.data_dir, "vocab.dat")
    vocab, rev_vocab = initialize_vocab(vocab_path)
    embedding = tf.constant(load_embeddings(embed_path), dtype = tf.float32)
    encoder = Encoder(FLAGS.state_size, FLAGS.max_context_len, FLAGS.max_question_len, FLAGS.embedding_size, FLAGS.summary_flag, FLAGS.filter_flag)
    decoder = Decoder(FLAGS.state_size, FLAGS.max_context_len, FLAGS.max_question_len, FLAGS.output_size, FLAGS.summary_flag)
    qa = QASystem(encoder, decoder, FLAGS, embedding, rev_vocab)

    with tf.Session() as sess:
        train_dir = get_normalized_train_dir(train_dir)
        qa = initialize_model(sess, qa, train_dir)
        output_list, output_dict = generate_answers(sess, qa, val_data, rev_vocab)
        store_result(output_list, output_dict, train_dir)

if __name__ == "__main__":
  tf.app.run()
