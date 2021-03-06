import numpy as np
import tensorflow as tf
import pdb
import sys
import pickle
import warnings
import time
import os

import NN
import NNAL_tools
from cvxopt import matrix, solvers

read_file_path = "/home/ch194765/repos/atlas-active-learning/AlexNet/"
sys.path.insert(0, read_file_path)
from alexnet import AlexNet

def test_MNIST(iters, B, k, init_size, batch_size, epochs, 
               train_dat=None, test_dat=None):
    """Evaluate active learning based on Fisher information,
    or equivalently expected change of the model, over MNIST
    data set
    """
    
    # preparing MNIST data set
    if not(train_dat):
        batch_of_data, batch_of_labels, pool_images, pool_labels, \
            test_images, test_labels = NNAL_tools.init_MNIST(init_size, batch_size)
    else:
        test_images = test_dat[0]
        test_labels = test_dat[1]
        batch_of_data, batch_of_labels, pool_images, pool_labels = \
            NNAL_tools.divide_training(train_dat, init_size, batch_size)
    
    # FI-based querying
    print("Doing FI-based querying")
    fi_accs, fi_data, fi_labels = \
        querying_iterations_MNIST(batch_of_data, batch_of_labels, 
                                  pool_images, pool_labels, 
                                  test_images, test_labels,
                                  iters, k, epochs, method="FI")

    print("Doing random querying")
    rand_accs, rand_data, rand_labels = \
        querying_iterations_MNIST(batch_of_data, batch_of_labels, 
                                  pool_images, pool_labels, 
                                  test_images, test_labels,
                                  iters, k, epochs, method="random")

    print("Doing uncertainty sampling")
    ent_accs, ent_data, ent_labels = \
        querying_iterations_MNIST(batch_of_data, batch_of_labels, 
                                  pool_images, pool_labels, 
                                  test_images, test_labels,
                                  iters, k, epochs, method="entropy")
            
    return fi_accs, rand_accs, ent_accs


def querying_iterations_MNIST(batch_of_data, batch_of_labels, 
                             pool_images, pool_labels, 
                             test_images, test_labels,
                             iters, k, epochs, method):
    
    c = pool_labels.shape[0]
    d = pool_images.shape[0]
    accs = np.zeros((c+1,iters+1))
    
    # initial training
    with tf.Session() as sess:
        
        print("Initializing the model...")
        
        # input and output placeholders
        x = tf.placeholder(tf.float32, shape=[d, None])
        y_ = tf.placeholder(tf.float32, shape=[10, None])

        # parameters
        W = tf.Variable(tf.zeros([10, d]))
        b = tf.Variable(tf.zeros([10,1]))
        
        # initializing
        sess.run(tf.global_variables_initializer())

        # outputs of the network
        y = tf.matmul(W,x) + b
        posteriors = tf.nn.softmax(tf.transpose(y))
        #log_posteriors = tf.log(posteriors)
        
        # cross entropy as the training objective
        cross_entropy = tf.reduce_mean(
            tf.nn.softmax_cross_entropy_with_logits(labels=tf.transpose(y_), 
                                                    logits=tf.transpose(y)))

        # optimization iteration
        train_step = tf.train.GradientDescentOptimizer(0.05).minimize(cross_entropy)

        # initial training
        for _ in range(epochs):
            for i in range(len(batch_of_data)):
                train_step.run(feed_dict={x: batch_of_data[i], 
                                          y_: batch_of_labels[i]})
        
        # initial accuracy
        correct_prediction = tf.equal(tf.argmax(y,0), tf.argmax(y_,0))
        accuracy = tf.reduce_mean(tf.cast(correct_prediction, tf.float32))
        accs[0,0] = accuracy.eval(feed_dict={x: test_images, 
                                             y_: test_labels})
        # accuracies in each class
        for j in range(1,c+1):
            indics = test_labels[j-1,:]==1
            accs[j,0] = accuracy.eval(feed_dict={x: test_images[:,indics], 
                                                 y_: test_labels[:,indics]})

        # start the querying iterations
        print("Starting the querying iterations..")
        added_labels = []
        #added_images = np.zeros((iters, d))
        for t in range(1, iters+1):
            
            if method=="FI":
                """FI-based querying"""
                # compute all the posterior probabilities
                pool_posteriors = sess.run(posteriors, feed_dict=
                                           {x: pool_images, y_: pool_labels})
                
                # using the normalized pool-samples
                pool_norms = np.sum(pool_images**2, axis=0)
                pool_norms /= pool_norms.max()
                
                # norm of posteriors
                pool_posteriors_norms = np.sum(pool_posteriors**2, axis=1)
                # scores
                scores = (pool_norms+1)*(1-pool_posteriors_norms)

                # take the best k scores
                #bests = np.argsort(-scores)[:100]
                #Q = np.array([bests[np.random.randint(100)]])
                Q = np.argsort(-scores)[:k]
                
            elif method=="random":
                """randomd querying"""
                Q = np.random.randint(0, pool_images.shape[1], k)
                
            elif method=="entropy":
                # compute all the posterior probabilities
                pool_posteriors = sess.run(posteriors, feed_dict=
                                           {x: pool_images, y_: pool_labels})
                entropies = NNAL_tools.compute_entropy(pool_posteriors.T)
                Q = np.argsort(-entropies)[:k]
            
            new_train_data = pool_images[:,Q]
            new_train_labels = pool_labels[:,Q]
            
            #added_images[t-1,:] = np.squeeze(new_train_data)
            added_labels += [np.where(new_train_labels)[0][0]]
            
            batch_of_data, batch_of_labels = \
                NNAL_tools.update_batches(batch_of_data, 
                                          batch_of_labels,
                                          new_train_data,
                                          new_train_labels,
                                          'regular')
            
            # fine-tuning
            sess.run(tf.global_variables_initializer())
            for _ in range(epochs):
                for i in range(len(batch_of_data)):
                    train_step.run(feed_dict={x: batch_of_data[i], 
                                              y_: batch_of_labels[i]})

            accs[0,t] = accuracy.eval(feed_dict={x: test_images, 
                                               y_: test_labels})
            # accuracies in each class
            for j in range(1,c+1):
                indics = test_labels[j-1,:]==1
                accs[j,t] = accuracy.eval(feed_dict={x: test_images[:,indics], 
                                                     y_: test_labels[:,indics]})
            # update the pool
            np.delete(pool_images, Q, 1)
            np.delete(pool_labels, Q, 1)
            
            nL = np.concatenate(batch_of_data, axis=1).shape[1]
            print("Iteration %d is done. Number of labels: %d" % (t, nL))
    
    return accs, batch_of_data, batch_of_labels

def CNN_query(model,
              expr,
              pool_inds,
              method_name,
              session,
              col=True,
              extra_feed_dict={}):
    """Querying a number of unlabeled samples from a given pool
    
    :Parameters:
    
      **model** : CNN model object
        any CNN class object which has methods, `output` as the 
        output of the network, and `posteriors` as the estimated
        posterior probability of the classes
        
      **k** : positive integer
        number of queries to be selected
        
      **B** : positive integer
        number of samples to keep in uncertainty filterins
        (only will be used in `egl` and `fi-` methods)
        
      **pool_X** : 4D tensors
        pool of unlabeled samples that is stored in format
        `[batch, rows, columns, n_channels]`
        
      **method** : string
        the querying method
        
      **session** : tf.Session()
        the tensorflow session operating on the model
        
      **batch_size** : integers (default is None)
        size of the batches for batch-wise computation of
        posteriors and gradients; if not provided, full data
        will be used at once in those computations, which is
        prone to out-of-memory error especially when GPU's
        are being used
    """
    
    k = expr.pars['k']
    B = expr.pars['B']
    lambda_ = expr.pars['lambda_']
    batch_size = expr.pars['batch_size']

    if method_name=='egl':
        # uncertainty filtering
        print("Uncertainty filtering...")
        posteriors = NNAL_tools.batch_posteriors(
            model, pool_inds, 
            img_path_list, 
            batch_size, 
            session, col, extra_feed_dict)
            
        if B < posteriors.shape[1]:
            sel_inds = NNAL_tools.uncertainty_filtering(posteriors, B)
            sel_posteriors = posteriors[:, sel_inds]
        else:
            B = posteriors.shape[1]
            sel_posteriors = posteriors
            sel_inds = np.arange(B)

        # EGL scoring
        print("Computing the scores..")
        c = posteriors.shape[0]
        scores = np.zeros(B)
        T = len(model.grad_log_posts['0'])
        for i in range(B):
            # gradients of samples one-by-one
            feed_dict = {model.x:np.expand_dims(
                    pool_X[sel_inds[i],:,:,:], 
                    axis=0)}
            feed_dict.update(extra_feed_dict)
            
            if c < 20:
                grads = session.run(
                    model.grad_log_posts, 
                    feed_dict=feed_dict)
                sel_classes = np.arange(c)
            else:
                # if the number of classes is large,
                # compute gradients of the largest twenty
                # posteriors
                sel_classes = np.argsort(
                    -sel_posteriors[:,i])[:10]
                sel_classes_grads = {
                    str(cc): model.grad_log_posts[str(cc)]
                    for cc in sel_classes
                    }
                grads = session.run(sel_classes_grads, 
                                    feed_dict=feed_dict)
                
            for j in range(len(sel_classes)):
                class_score = 0.        
                for t in range(T):
                    class_score += np.sum(
                        grads[str(sel_classes[j])][t]**2)
                    scores[i] += class_score*sel_posteriors[
                        sel_classes[j],i]
            if not(i%10):
                print(i, end=',')

        # select the highest k scores
        Q_inds = sel_inds[np.argsort(-scores)[:k]]

    elif method_name=='random':
        n = len(pool_inds)
        Q_inds = np.random.permutation(n)[:k]
        
    elif method_name=='entropy':
        # computing the posteriors
        posteriors = NNAL_tools.idxBatch_posteriors(
            model, 
            pool_inds,
            expr,
            session, 
            col, 
            extra_feed_dict)
        
        # entropies    
        entropies = NNAL_tools.compute_entropy(posteriors)
        Q_inds = np.argsort(-entropies)[:k]
        
    elif method_name=='fi':
        # uncertainty filtering
        print("Uncertainty filtering...", end='\n\t')

        posteriors = NNAL_tools.idxBatch_posteriors(
            model, 
            pool_inds, 
            expr, 
            session, 
            col, 
            extra_feed_dict)
        
        # uncertainty filtering
        if B < posteriors.shape[1]:
            sel_inds = NNAL_tools.uncertainty_filtering(
                posteriors, B)
            sel_posteriors = posteriors[:, sel_inds]
        else:
            B = posteriors.shape[1]
            sel_posteriors = posteriors
            sel_inds = np.arange(B)
            
        # forming A-matrices
        # division by two in computing size of A is because 
        # in each layer we have gradients with respect to
        # weights and bias terms --> number of layers that
        # are considered is obtained after dividing by 2
        A_size = int(
            len(model.grad_posts['0'])/2)
        c,n = posteriors.shape

        A = []
        # load an images
        # indices: sel_inds --> pool_inds
        # CAUTIOUS: this will give an error if the selected
        # indices in `sel_inds` contains only one index.
        sel_X, _ = NN.load_winds(
            pool_inds[sel_inds],
            expr.imgs_path_file,
            expr.pars['target_shape'],
            expr.pars['mean'])
        
        for i in range(B):
            X_i = sel_X[i,:,:,:]
            feed_dict = {
                model.x:np.expand_dims(X_i, axis=0)}
            feed_dict.update(extra_feed_dict)

            # remove zero, or close-to-zero posteriors
            x_posterior = sel_posteriors[:,i]
            x_posterior[x_posterior<1e-6] = 0.
            nz_classes = np.where(x_posterior > 0.)[0]
            nz_posts = x_posterior[nz_classes] / np.sum(
                x_posterior[nz_classes])
            nz_classes_grads = {
                str(cc): model.grad_posts[str(cc)]
                for cc in nz_classes}
            
            # computing the gradients
            # grads={ '0': dP(y=0|x)/dtheta, 
            #         '1': dP(y=1|x)/dtheta, 
            #         etc }
            # if there are too many classes, 
            # grads={ 'c0': dP(y=c0|x)/dtheta, 
            #         'c1': dP(y=c1|x)/dtheta, 
            #         etc }
            # where {c0,c1,etc} are classes with largest
            # posteriors for x.
            # 
            if len(nz_classes) < 10:
                grads = session.run(nz_classes_grads, 
                                    feed_dict=feed_dict)
                sel_classes = nz_classes
                new_posts = nz_posts
            else:
                # if the number of classes is large,
                # compute gradients of few classes with 
                # largest  posteriors only
                sel_nz_classes = np.argsort(-nz_posts)[:10]
                sel_classes = nz_classes[sel_nz_classes]
                sel_classes_grads = {
                    str(cc): nz_classes_grads[str(cc)]
                    for cc in sel_classes}
                # normalizing posteriors of the selected classes
                new_posts = nz_posts[sel_nz_classes]
                new_posts /= np.sum(new_posts)
                # gradients for the selected classes
                grads = session.run(sel_classes_grads, 
                                    feed_dict=feed_dict)

            Ai = np.zeros((A_size, A_size))
            
            for j in range(len(sel_classes)):
                shrunk_grad = NNAL_tools.shrink_gradient(
                    grads[str(sel_classes[j])], 'sum')
                Ai += np.outer(shrunk_grad, 
                               shrunk_grad) / new_posts[j] \
                    + np.eye(A_size)*1e-5

            if not(i%10):
                print(i, end=',')
            
            A += [Ai]
            
        # extracting features for pool samples
        # using only few indices of the features
        F = model.extract_features(pool_inds[sel_inds], 
                                   expr,session)
        # selecting from those features that have the most
        # non-zero values among the selected samples
        nnz_feats = np.sum(F>0, axis=1)
        feat_inds = np.argsort(-nnz_feats)[:int(B/2)]
        F_sel = F[feat_inds,:]
        # taking care of the rank
        while np.linalg.matrix_rank(F_sel)<len(feat_inds):
            # if the matrix is not full row-rank, discard
            # the last selected index (worst among all)
            feat_inds = feat_inds[:-1]
            F_sel = F[feat_inds,:]
            if len(feat_inds) < 10:
                warnings.warn(
                    "Few features (%d) are selected"% (
                        len(feat_inds)))
                
        # taking care of the conditional number
        while np.linalg.cond(F_sel) > 1e6:
            feat_inds = feat_inds[:-1]
            F_sel = F[feat_inds,:]
            if len(feat_inds)==1:
                lambda_=0
                break
        
        #pdb.set_trace()
        # subtracting the mean
        F_sel -= np.repeat(np.expand_dims(
            np.mean(F_sel, axis=1),
            axis=1), B, axis=1)
        
        print('Cond. #: %f'% (np.linalg.cond(F_sel)),
              end='\n\t')
        print('# selected features: %d'% 
              (len(feat_inds)), end='\n\t')
        # SDP
        print('Solving SDP..',end='\n\t')
        soln = NNAL_tools.SDP_query_distribution(
            A, lambda_, F_sel, k)
        print('status: %s'% (soln['status']), end='\n\t')
        q_opt = np.array(soln['x'][:B])
        
        # sampling from the optimal solution
        Q_inds = NNAL_tools.sample_query_dstr(
            q_opt, k, replacement=True)
        Q_inds = sel_inds[Q_inds]
        
    elif method_name=='rep-entropy':
        # uncertainty filtering
        print("Uncertainty filtering...")
        posteriors = NNAL_tools.idxBatch_posteriors(
            model, 
            pool_inds, 
            expr, 
            session, 
            col, 
            extra_feed_dict)
        
        if B < posteriors.shape[1]:
            sel_inds = NNAL_tools.uncertainty_filtering(
                posteriors, B)
            sel_posteriors = posteriors[:, sel_inds]
        else:
            B = posteriors.shape[1]
            sel_posteriors = posteriors
            sel_inds = np.arange(B)
            
        n = len(pool_inds)
        rem_inds = list(set(np.arange(n)) - set(sel_inds))
        
        print("\t Finding Similarities..", end='\n\t')
        # extract the features for all the pool
        # sel_inds, rem_inds  -->  pool_inds
        F = model.extract_features(pool_inds, 
                                   expr,
                                   session)
        F_uncertain = F[:, sel_inds]
        norms_uncertain = np.sqrt(np.sum(F_uncertain**2, axis=0))
        F_rem_pool = F[:, rem_inds]
        norms_rem = np.sqrt(np.sum(F_rem_pool**2, axis=0))
        
        # compute cos-similarities between filtered images
        # and the rest of the unlabeled samples
        dots = np.dot(F_rem_pool.T, F_uncertain)
        norms_outer = np.outer(norms_rem, norms_uncertain)
        sims = dots / norms_outer
            
        print("Greedy optimization..", end='\n\t')
        # start from empty set
        Q_inds = []
        nQ_inds = np.arange(B)
        # add most representative samples one by one
        for i in range(k):
            rep_scores = np.zeros(B-i)
            for j in range(B-i):
                cand_Q = Q_inds + [nQ_inds[j]]
                rep_scores[j] = np.sum(
                    np.max(sims[:, cand_Q], axis=1))
            iter_sel = nQ_inds[np.argmax(rep_scores)]
            # update the iterating sets
            Q_inds += [iter_sel]
            nQ_inds = np.delete(
                nQ_inds, np.argmax(rep_scores))
            
        Q_inds = sel_inds[Q_inds]

    return Q_inds

def run_CNNAL(A, init_X_train, init_Y_train,
              X_pool, Y_pool, X_test, Y_test, epochs, 
              k, B, method, max_queries, train_batch=50, 
              eval_batch=None):
    """Starting with a CNN model that is trained with an initial
    labeled data set, and then perform certain number of querying 
    iterations using a specified active learning method
    """
    
    test_acc = []
    saver = tf.train.Saver()

    with tf.Session() as session:
        saver.restore(session, A.save_path)
        test_acc += [A.accuracy.eval(feed_dict={
                    A.x: X_test, A.y_:Y_test})]
        print()
        print('Test accuracy: %g' %test_acc[0])

        # start querying
        new_X_train, new_Y_train = init_X_train, init_Y_train
        new_X_pool, new_Y_pool = X_pool, Y_pool
        A.get_gradients()
        # number of selected in each iteration is useful
        # when samling from a distribution and repeated
        # queries might be present
        query_num = []
        print(20*'-' + '  Querying  ' +20*"-")
        t = 0
        while sum(query_num) < max_queries:
            print("Iteration %d: "% t)
            Q_inds = CNN_query(A, k, B, new_X_pool, 
                               method, session, eval_batch)
            query_num += [len(Q_inds)]
            print('Query index: '+' '.join(str(q) for q in Q_inds))
            # prepare data for another training
            Q = new_X_pool[Q_inds,:,:,:]
            #pickle.dump(Q, open('results/%s/%d.p'% (method,t),'wb'))
            Y_Q = new_Y_pool[:,Q_inds]
            # remove the selected queries from the pool
            new_X_pool = np.delete(new_X_pool, Q_inds, axis=0)
            new_Y_pool = np.delete(new_Y_pool, Q_inds, axis=1)
            # update the model
            print("Updating the model: ", end='')
            new_X_train, new_Y_train = NNAL_tools.prepare_finetuning_data(
                new_X_train, new_Y_train, Q, Y_Q, 200+t, 50)
            for i in range(epochs):    
                A.train_graph_one_epoch(new_X_train, new_Y_train, 
                                        train_batch, session)
                print(i, end=', ')
            
            test_acc += [A.accuracy.eval(
                    feed_dict={A.x: X_test, A.y_:Y_test})]
            print()
            print('Test accuracy: %g' %test_acc[t+1])
            t += 1
            
    return np.array(test_acc), np.append(0, np.array(query_num))
            

def run_AlexNet_AL(X_pool, Y_pool, X_test, Y_test,
                   learning_rate, dropout_rate, epochs, 
                   k, B, methods, max_queries, 
                   train_batch_size, 
                   model_save_path,
                   results_save_path, 
                   index_save_path=None,
                   eval_batch_size=None,
                   init_train_dat=None):
    """Running active learning algorithms on a
    pre-trained AlexNet
    
    This function is written separate than `run_CNNAL`, because
    the architecture of AlexNet cannot be modelled by our 
    current generic CNN class at this time. It is mainly
    because AlexNet has more than two groups in some 
    convolutional layers, where the input is cut in half
    and same or different filters are used in each group
    to output a feature map. 
    
    Hence, we are using a publicly available piece of code,
    which is written by Frederik Kratzert in his blog 
    https://kratzert.github.io/2017/02/24/finetuning-alexnet-
    with-tensorflow.html
    for fine-tuning pre-trained AlexNet in TensorFlow given 
    any labeled data set.
    """

    # layers we don't wanna modify in the fine-tuning process
    skip_layer = ['fc8']
    
    # path to the pre-trained weights
    weights_path = '/home/ch194765/repos/atlas-active-learning/AlexNet/bvlc_alexnet.npy'
    
    # creating the AlexNet mode
    # -------------------------
    # preparing variables
    c = Y_pool.shape[1]
    
    if os.path.isfile('%s/results.dat'% index_save_path):
        print('Some results already exist..')
        accs, fi_queries = pickle.load(
            '%s/results.dat'% index_save_path)
    else:
        accs = {method:[] for method in methods}
        fi_query_num = [0]
    
    tf.reset_default_graph()
    x = tf.placeholder(tf.float32, 
                       [None, 227, 227, 3])
    # creating the model
    model = NN.AlexNet_CNN(
        x, dropout_rate, c, skip_layer, weights_path)
    model.get_optimizer(learning_rate)
    # getting the gradient operations
    model.get_gradients(5)
    saver = tf.train.Saver()

    with tf.Session() as session:

        # initialization
        model.initialize_graph(session)
        
        # if an initial training data is given..
        if init_train_dat:
            print("Initializing the model")
            init_X_train = init_train_dat[0]
            init_Y_train = init_train_dat[1]
            for i in range(epochs):
                model.train_graph_one_epoch(
                    init_X_train, init_Y_train, 
                    train_batch_size, session)
                
        if os.path.isfile(model_save_path+'.index'):
            # load the graph
            saver.restore(session, model_save_path)
        else:
            # save the graph
            saver.save(session, model_save_path)
            
        session.graph.finalize()
            
        init_acc = NNAL_tools.batch_accuracy(
                model, X_test, Y_test, 
                eval_batch_size, session, col=False)
        
        extra_feed_dict = {model.KEEP_PROB: model.dropout_rate}
        for M in methods:
            print('Test accuracy: %g' %init_acc)
            
            if os.path.exists('%s/%s'% (index_save_path, M)):
                continue
            
            if M=='fi':
                accs[M] += [init_acc]
            else:
                accs[M] = np.zeros(int(max_queries/k)+1)
                accs[M][0] = init_acc
                
            if not(M==methods[0]):
                saver.restore(session, model_save_path)
                
            # start querying
            if init_train_dat:
                X_train = init_X_train
                Y_train = init_Y_train
            else:
                X_train = np.zeros((0,)+X_pool.shape[1:])
                Y_train = np.zeros((0,c))

            # number of selected in each iteration is useful
            # when samling from a distribution and repeated
            # queries might be present
            query_num = 0
            print(20*'-' + '  Querying  ' +20*"-")
            t = 0
            while query_num < max_queries:
                #T1 = time.time()
                print("Iteration %d: "% t)
                Q_inds = CNN_query(model, k, B, X_pool, 
                                   M, session, eval_batch_size, 
                                   False, extra_feed_dict)
                query_num += len(Q_inds)
                # save the queries if necessary:
                if index_save_path:
                    # create the path if necessary
                    if not(os.path.exists('%s/%s'% (index_save_path, M))):
                        os.mkdir('%s/%s'% (index_save_path, M))
                    # the query indices are based on rows of
                    # pool_inds.txt
                    np.savetxt(
                        '%s/%s/Q-%d.txt'% (index_save_path, M, t), 
                        Q_inds, fmt='%d')
                
                print('Query index: '+' '.join(str(q) for q in Q_inds))
                # prepare data for another training
                Q = X_pool[Q_inds,:,:,:]
                Y_Q = Y_pool[Q_inds,:]
                # remove the selected queries from the pool
                X_pool = np.delete(X_pool, Q_inds, axis=0)
                Y_pool = np.delete(Y_pool, Q_inds, axis=0)
                # update the model
                print("Updating the model: ", end='')
                X_train, Y_train = NNAL_tools.prepare_finetuning_data(
                    X_train, Y_train.T, 
                    Q, Y_Q.T, 200+t, train_batch_size)
                Y_train = Y_train.T
                for i in range(epochs):
                    model.train_graph_one_epoch(
                        X_train, Y_train, 
                        train_batch_size, session)
                    print(i, end=', ')

                print()
                #T2 = time.time()
                #dT = (T2 - T1) / 60
                #print("This iteration took %f m"% dT)
                
                iter_acc = NNAL_tools.batch_accuracy(
                    model, X_test, Y_test, 
                    eval_batch_size, session, col=False)
                
                t += 1
                if M=='fi':
                    accs[M] += [iter_acc]
                    fi_query_num += [len(Q_inds)]
                else:
                    accs[M][t] = iter_acc
                    
                print('Test accuracy: %g' % iter_acc)
                
            #pdb.set_trace()
            pickle.dump([accs, fi_query_num], 
                        open(results_save_path, 'wb'))

    return accs, fi_query_num
