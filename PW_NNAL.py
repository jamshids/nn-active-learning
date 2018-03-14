from skimage.measure import regionprops
import tensorflow as tf
import numpy as np
import warnings
import nibabel
import nrrd
import pdb
import os

import NNAL_tools
import PW_NN
import PW_AL
import patch_utils


def CNN_query(expr,
              model,
              sess,
              padded_imgs,
              pool_inds,
              tr_inds,
              method_name):
    """Querying strategies for active
    learning of patch-wise model
    """

    if method_name=='random':
        n = len(pool_inds)
        q = np.random.permutation(n)[
            :expr.pars['k']]

    if method_name=='entropy':
        # posteriors
        posts = PW_NN.batch_eval(
            model,
            sess,
            padded_imgs,
            pool_inds,
            expr.pars['patch_shape'],
            expr.pars['ntb'],
            expr.pars['stats'],
            'posteriors')[0]
        
        # k most uncertain (binary classes)
        q = np.argsort(np.abs(posts-.5))[
            :expr.pars['k']]
        
    if method_name=='rep-entropy':
        ####### OUT-DATED
        # posteriors
        posts = PW_AL.batch_eval_wlines(
            expr,
            run,
            model, 
            pool_inds,
            'posteriors',
            sess)
        
        # vectories everything
        # uncertainty filtering
        B = expr.pars['B']
        if B < len(posts):
            sel_inds = np.argsort(
                np.abs(posts-.5))[:B]
            sel_posts = posts[sel_inds]
        else:
            B = posts.shape[1]
            sel_posts = posts
            sel_inds = np.arange(B)
            
        n = len(pool_inds)
        rem_inds = list(set(np.arange(n)) - 
                        set(sel_inds))
        
        # extract the features for all the pool
        # sel_inds, rem_inds  -->  pool_inds
        F = PW_AL.batch_eval_wlines(
            expr,
            run,
            model,
            pool_inds,
            'feature_layer',
            sess)

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
        for i in range(expr.pars['k']):
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
            
        q = sel_inds[Q_inds]

    if method_name=='fi':
        # posteriors
        posts = PW_NN.batch_eval(
            model,
            sess,
            padded_imgs,
            pool_inds,
            expr.pars['patch_shape'],
            expr.pars['ntb'],
            expr.pars['stats'],
            'posteriors')[0]
        
        # vectories everything
        # uncertainty filtering
        B = expr.pars['B']
        if B < len(pool_inds):
            sel_inds = np.argsort(
                np.abs(posts-.5))[:B]
            sel_posts = posts[sel_inds]
        else:
            B = posts.shape[1]
            sel_posts = posts
            sel_inds = np.arange(B)

        #sel_posts = PW_AL.batch_eval_wlines(
        #    expr,
        #    run,
        #    model, 
        #    pool_inds[sel_inds],
        #    'posteriors',
        #    sess)

        # forming A-matrices
        # ------------------
        # division by two in computing size of A is because 
        # in each layer we have gradients with respect to
        # weights and bias terms --> number of layers that
        # are considered is obtained after dividing by 2
        A_size = int(
            len(model.grad_posts['1'])/2)
        n = len(pool_inds)
        c = expr.nclass

        A = []
        # load the patches
        # indices: sel_inds --> pool_inds
        # CAUTIOUS: this will give an error if 
        # the selected indices in `sel_inds`
        # contains only one index.
        sel_patches = patch_utils.get_patches(
            padded_imgs, pool_inds[sel_inds],
            expr.pars['patch_shape'])
            
        for i in range(B):
            X_i = (sel_patches[i,:,:,:]-
                   expr.pars['stats'][0]) / \
                expr.pars['stats'][1]
            feed_dict = {
                model.x: np.expand_dims(X_i,axis=0),
                model.keep_prob: 1.}

            # preparing the poserior
            # ASSUMOTION: binary classifications
            x_post = sel_posts[i]
            # Computing gradients and shrinkage
            if x_post < 1e-6:
                x_post = 0.

                grads_0 = sess.run(
                    model.grad_posts['0'],
                    feed_dict=feed_dict)

                grads_0 =  NNAL_tools.\
                           shrink_gradient(
                               grads_0, 'sum')
                grads_1 = 0.

            elif x_post > 1-1e-6:
                x_post = 1.

                grads_0 = 0.

                grads_1 = sess.run(
                    model.grad_posts['1'],
                    feed_dict=feed_dict)

                grads_1 = NNAL_tools.\
                          shrink_gradient(
                              grads_1, 'sum')
            else:
                grads_0 = sess.run(
                    model.grad_posts['0'],
                    feed_dict=feed_dict)
                grads_0 =  NNAL_tools.\
                           shrink_gradient(
                               grads_0, 'sum')

                grads_1 = sess.run(
                    model.grad_posts['1'],
                    feed_dict=feed_dict)
                grads_1 =  NNAL_tools.\
                           shrink_gradient(
                               grads_1, 'sum')
                
            # the A-matrix
            Ai = (1.-x_post) * np.outer(grads_0, grads_0) + \
                 x_post * np.outer(grads_1, grads_1)
                
            # final diagonal-loading
            A += [Ai+ np.eye(A_size)*1e-5]

            if not(i%10):
                print(i, end=',')

        # extracting features for pool samples
        # using only few indices of the features
        F = PW_NN.batch_eval(model,
                             sess,
                             padded_imgs,
                             pool_inds[sel_inds],
                             expr.pars['patch_shape'],
                             expr.pars['ntb'],
                             expr.pars['stats'],
                             'feature_layer')[0]

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
                
        # taking care of the conditional number
        lambda_ = expr.pars['lambda_']
        while np.linalg.cond(F_sel) > 1e6:
            feat_inds = feat_inds[:-1]
            F_sel = F[feat_inds,:]
            if len(feat_inds)==1:
                lambda_=0
                print('Only one feature is selected.')
                break
        
        # subtracting the mean
        F_sel -= np.repeat(np.expand_dims(
            np.mean(F_sel, axis=1),
            axis=1), B, axis=1)
        
        print('Cond. #: %f'% (np.linalg.cond(F_sel)),
              end='\n\t')
        print('# selected features: %d'% 
              (len(feat_inds)), end='\n\t')
        
        # SDP
        # ----
        soln = NNAL_tools.SDP_query_distribution(
            A, lambda_, F_sel, expr.pars['k'])
        print('status: %s'% (soln['status']), end='\n\t')
        q_opt = np.array(soln['x'][:B])
        
        # sampling from the optimal solution
        Q_inds = NNAL_tools.sample_query_dstr(
            q_opt, expr.pars['k'], 
            replacement=True)
        q = sel_inds[Q_inds]

    elif method_name=='prob-entropy':
        # posteriors
        posts = PW_AL.batch_eval_winds(
            expr,
            run,
            model, 
            pool_inds,
            'posteriors',
            sess)

        # extracting features
        pdb.set_trace()
        pool_F = PW_AL.batch_eval_winds(
            expr,
            run,
            model,
            pool_inds,
            'feature_layer',
            sess)

        if len(tr_inds)>0:
            tr_F = PW_AL.batch_eval_winds(
                expr,
                run,
                model,
                tr_inds,
                'feature_layer',
                sess)
        else:
            tr_F = []

        # self-similarities
        U_self_sims = get_self_sims(pool_F)
        # unlabeled-labeled similarities
        if tr_F:
            UL_sims = get_cross_sims(pool_F,
                                     tr_F)
        
        # forming the distributions
        P_1 =  np.exp(-pis[0]*(posts-0.5)**2)
        P_1 = P_1 / np.sum(P_1)
        P_2 = np.exp(-pis[1]*U_self_sims)
        P_2 = P_2 / np.sum(P_2)
        if tr_F:
            P_3 = np.exp(-pis[2]/UL_sims)
            P_3 = P_3 / np.sum(P_3)
        else:
            P_3 = 1.

        # multiplicative mixture
        pis = expr.pars['q_mixing_coeffs']
        logq = np.log(P_1) + \
               np.log(P_2) + \
               np.log(P_3)
        q = np.exp(logq)
        # taking care of zero values
        z_indic = np.logical_or(
            P_1==0,P_2==0)
        z_indic = np.logical_or(
            z_indic, P_3==0)
        q[z_indic] = 0
        # re-normalization
        q = q / np.sum(q)

        # now sampling from this distribution
        # -----
        # update the distribution every `b`
        # samples to incorporate diversity
        b = 50
        k = expr.pars['k']
        prior = []
        draw_inds = np.arange(0, k, b)
        if not(draw_inds[-1]==k):
            draw_inds = np.append(
                draw_inds, k)

        Q_inds = np.zeros(k)
        for i in range(len(draw_inds)-1):
            iter_draws = draw_queries(
                qdist, priors)
            Q_inds[draw_inds[i]:
                   draw_inds[i+1]] = iter_draws
            
            if i<len(draw_inds)-2:
                # updating the prior using the
                # similarity between the pool
                # and samples that are selected
                # so far
                F_Q = pool_F[:,iter_draws]
                QU_sims = get_cross_sims(
                    pool_F, F_Q)
                prior = np.exp(-pis[1]*QU_sim)
                prior[iter_draws] = 0
                prior = prior / np.sum(prior)
            
        q = Q_inds
        
    return q


def SuPix_query(expr,
                run,
                model,
                pool_lines,
                train_inds,
                overseg_img,
                method_name,
                sess):
    """Querying strategies for active
    learning of patch-wise model
    """

    k = expr.pars['k']

    if method_name=='random':
        n = len(pool_lines)
        q = np.random.permutation(n)[:k]

    if method_name=='entropy':
        # posteriors
        posts = PW_AL.batch_eval_wlines(
            expr,
            run,
            model,
            pool_lines,
            'posteriors',
            sess)
        
        # explicit entropy scores
        scores = np.abs(posts-.5)

        # super-pixel scores
        inds_path = os.path.join(
            expr.root_dir, str(run),
            'inds.txt')
        inds_dict, locs_dict = PW_AL.create_dict(
            inds_path, pool_lines)
        pool_inds = inds_dict[list(
            inds_dict.keys())[0]]
        SuPix_scores = superpix_scoring(
            overseg_img, pool_inds, scores)
        
        # argsort-ing is not sensitive to 
        # NaN's, so invert np.inf to np.nan
        SuPix_scores[
            SuPix_scores==np.inf]=np.nan
        # also nan-out the zero superpixels
        qSuPix = np.unravel_index(
            np.argsort(np.ravel(SuPix_scores)), 
            SuPix_scores.shape)
        qSuPix = np.array([qSuPix[0][:k],
                           qSuPix[1][:k]])

    # when the superpixels are selecte, 
    # extract their grid-points too
    qSuPix_inds = PW_AL.get_SuPix_inds(
        overseg_img, qSuPix)

    return qSuPix, qSuPix_inds

def binary_uncertainty_filter(posts, B):
    """Uncertainty filtering for binary class
    label distribution
    
    Since there are only two classes, posterior
    probability of only one of the classes
    are given in form of 1D array.
    """
    
    return np.argsort(np.abs(
        np.array(posts)-0.5))[:B]

def superpix_scoring(overseg_img,
                     inds,
                     scores):
    """Extending scores of a set of pixels
    represented by line numbers in index file,
    to a set of overpixels in a given
    oversegmentation
    
    :Parameters:
    
        **overseg_img** : 3D array
            oversegmentation of the image
            containing super-pixels

        **inds** : 1D array-like
            3D index of the pixels that are
            socred

        **socres** : 1D array-like
            scores that are assigned to pixels
    
    :Returns:

        **SuPix_scores** : 2D array
            scores assigned to super-pixels, 
            where each row corresponds to a
            slice of the image, and each 
            column corresponds to a super-pixel;
            such that the (i,j)-th element 
            represents the score assigned to
            the super-pixel with label j in 
            the i-th slice of the over-
            segmentation image

            If the (i,j)-th element is `np.inf`
            it means that the super-pixel with
            label j in slice i did not get any
            score pixel in its area. And if
            it is assigned zero, it means that 
            the superpixel with label j does
            not exist in slice i at all.
    """
    
    # multi-indices of pixel indices
    s = overseg_img.shape
    multinds = np.unravel_index(inds, s)
    Z = np.unique(multinds[2])
    
    SuPix_scores = np.ones(
        (s[2], 
         int(overseg_img.max()+1)))*np.inf
    for z in Z:
        slice_ = overseg_img[:,:,z]

        """ Assigning Min-Itensity of Pixels """
        # creatin an image with 
        # values on the location of 
        # pixels
        score_img = np.ones(slice_.shape)*\
                    np.inf
        slice_indic = multinds[2]==z
        score_img[
            multinds[0][slice_indic],
            multinds[1][slice_indic]]=scores[
                slice_indic]
        # now take the properties of 
        # superpixels according to the
        # score image
        props = regionprops(slice_, 
                            score_img)
        # storing the summary score
        for i in range(len(props)):
            # specify which property to keep
            # as the scores summary
            SuPix_scores[z,props[i]['label']] \
                = props[i]['min_intensity']

    return SuPix_scores
    
def draw_queries(qdist, prior, k,
                 replacement=False):
    """Drawing query samples from a query
    distribution, and possible a prior
    priobability
    """
    
    if len(prior)==0:
        pies = qdist
    else:
        pies = qdist*prior

    # returning sampled indices
    Q_inds = NNAL_tools.sample_query_dstr(
        pies, k, replacement)

    return Q_inds

def get_self_sims(F):
    """Computing representativeness of
    all members of a set described by
    the given feature vectors

    The given argument should be a 2D
    matrix, such that the i'th column
    represents features of the i'th 
    sample in the set.
    """
    
    # size of the chunk for computing
    # pairwise similarities
    b = 5000
    n = F.shape[1]
    
    # dividing indices into chunks
    ind_chunks = np.arange(
        0, n, b)
    if not(ind_chunks[-1]==n):
        ind_chunks = np.append(
            ind_chunks, n)

    reps = np.zeros(n)
    for i in range(len(ind_chunks)-1):
        Fp = F[:,ind_chunks[i]:
               ind_chunks[i+1]]
        chunk_size = ind_chunks[i+1]-\
                     ind_chunks[i]
        
        norms_p = np.sqrt(np.sum(
            Fp**2, axis=0))
        norms = np.sqrt(np.sum(
            F**2, axis=0))
        # inner product
        dots = np.dot(Fp.T, F)
        # outer-product of norms to
        # be used in the denominator
        norms_outer = np.outer(
            norms_p, norms)
        sims = dots / norms_outer
        
        # make the self-similarity 
        # -inf to ignore it
        sims[np.arange(chunk_size),
             np.arange(
                 ind_chunks[i],
                 ind_chunks[i+1])] = -np.inf
        
        # loading similarities
        reps[ind_chunks[i]:
             ind_chunks[i+1]] = np.max(
                 sims, axis=1)

    return reps

def get_cross_sims(F1, F2):
    """Computing similarities between
    individual members of  one set and 
    another set
    """

    b  = 5000
    n1 = F1.shape[1]
    n2 = F2.shape[1]

    # dividing indices into chunks
    ind_chunks = np.arange(
        0, n1, b)
    if not(ind_chunks[-1]==n1):
        ind_chunks = np.append(
            ind_chunks, n1)

    reps = np.zeros(n1)
    for i in range(len(ind_chunks)-1):
        Fp1 = F1[:,ind_chunks[i]:
                 ind_chunks[i+1]]
        
        norms_p1 = np.sqrt(np.sum(
            Fp1**2, axis=0))
        norms_2 = np.sqrt(np.sum(
            F2**2, axis=0))
        # inner product
        dots = np.dot(Fp1.T, F2)
        # outer-product of norms to
        # be used in the denominator
        norms_outer = np.outer(
            norms_p1, norms_2)
        sims = dots / norms_outer

        # loading the parameters
        reps[ind_chunks[i]:
             ind_chunks[i+1]] = np.max(
                 sims, axis=1)

    return reps    

def get_confident_samples(expr,
                          run,
                          model,
                          pool_inds,
                          num,
                          sess):
    """Generating a set of confident samples
    together with their labels
    """
    
    # posteriors
    posts = PW_AL.batch_eval_winds(
        expr,
        run,
        model,
        pool_inds,
        'posteriors',
        sess)
        
    # most confident samples
    conf_loc_inds = np.argsort(
        -np.abs(posts-.5))[:num]
    conf_inds = pool_inds[conf_loc_inds]
    
    # preparing their labels
    conf_labels = np.zeros(num, 
                           dtype=int)
    conf_labels[posts[conf_loc_inds]>.9]=1
    
    # counting number of mis-labeling
    inds_path = os.path.join(
        expr.root_dir, str(run), 'inds.txt')
    labels_path = os.path.join(
        expr.root_dir, str(run), 'labels.txt')
    inds_dict, labels_dict, locs_dict = PW_AL.create_dict(
        inds_path, conf_inds, labels_path)
    true_labels=[]
    for path in list(labels_dict.keys()):
        true_labels += list(labels_dict[path][
            locs_dict[path]])

    mis_labels = np.sum(~(
        true_labels==conf_labels))
    
    return conf_inds, conf_labels, mis_labels
        
        
        
