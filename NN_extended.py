import numpy as np
import tensorflow as tf
from tensorflow.examples.tutorials.mnist import input_data
from sklearn.metrics import f1_score
from skimage.util import random_noise
from scipy.ndimage import rotate
import linecache
import copy
import h5py
import pdb
import sys
#import cv2
import os

#from eval_utils import eval_metrics
import PW_NN
import AL


class CNN(object):
    """Class of CNN models
    """

    DEFAULT_HYPERS = {
        # general parameters
        'activation': 'ReLU',
        'custom_getter': None,
        'loss_name': 'CE',
        'optimizer_name': 'SGD',
        'lr_schedule': lambda t: exponential_decay(1e-3,t,0.1),
        'regularizer': None,
        'weight_decay': 1e-4,

        # imbalanced optimization
        'bin_class_weights': None,
        'focal_gamma': None,

        # batch normalization
        'BN_decay': 0.999,
        'BN_epsilon': 1e-3,

        # Adam optimizer
        'beta1': 0.9,
        'beta2': 0.999,
        # RMSProp optimizer
        'decay': 0.9,
        'momentum': 0.,
        'epsilon': 1e-10,

        # Aleatoric uncertainty
        'AU_4L': False,  # for labeled samples
        'AU_4U': False,  # for unlabeled samples
        'AU_log_rel_coeff': 0.1,
        'MC_T': 10,      # for classification AU
        # Mean Teacher semi-supervised
        'MT_SSL': False,
        'MT_ema_decay_schedule': lambda: tf.constant(0.999),
        'rotation_angle': None,
        'Gaussian_noise_std': None,
        'output_perturbation_measure': 'CE',
        'max_cons_coeff': 1e-3,
        'rampup_length': 5000
    }
    
    def __init__(self, 
                 x, 
                 layer_dict, 
                 name,
                 skips=[],
                 feature_layer=None,
                 dropout=None,
                 probes=[[],[]],
                 **kwargs):
        """Constructor takes the input placehoder, a dictionary
        whose keys are names of the layers and the items assigned to each
        key is a 2-element list inlcuding  depth of this layer and its type,
        and a name which will be assigned to the scope name of the variabels
        
        The constructor goes through all the layers one-by-one in the same
        order as the items of `layer_dict` dictionary, and add each layer
        over the previous one. Each time the layers is added the output of
        the model (stored in `self.output`) will be updated to be the output
        of the last layer. Hence, when we added a layer with an index equal
        to the given `feature_layer`, the marker `self.features` will be 
        make equal to the output of this layer. Moreover, if the dropout
        is supposed to be applied on this layer, the output of this layer
        will be dropped-out with the given probability, at the time of 
        training.
        
        The assumption is that at least the first layer is a CNN, hence
        depth of the input layer is the number of channels of the input.
        It is further assumed that the last layer of the network is
        not a CNN.
        
        Also, there is the option of specifying a layer whose output
        could be used extracted feature vectors of the input samples.
        
        :Parameters:
        
            **x** : Tensorflow placeholder in format [n_batch, (H, W), n_channel]
                Input to the network 
        
            **layer_dict** : dictionary
                Information about all layers of the network in format 
            
                    {layer_name: [layer_type, layer_specs, operation_order]}

                Layer's specifications, in turn, contains two or more
                items dependending on the type of the layer. At this time
                this class supports only three types of layers:
               
                - Convolutional:   [# output channel, kernel size, strides, padding]
                - Transpose Convolutional:
                                   [# output channel, kernel size, strides]
                - Fully-connected: [# output channel]
                - max-pooling:     [pool size]
                
                 Note that "kernel size", "strides" and "pool size" are a list with 
                 two elements, and "padding" is a string. For now the class
                 only supports `SAME` padding for transpose 2D-convolution; also
                 the second and third elements of `strides` for this layer
                 specifies height and width of the output tensor as 
                 `width=strides[1]*input.shape[1]`
                 `height=strides[2]*input.shape[2]`
        
            **name**: string
                Name of Tensorflow scope of all the variables defined in
                this class.

            **skips** : list
                List of skip connections; each element is a list of 
                three elements: [layer_index, list_of_destinations, skip_type]
                which indicates that outputs of the layer_index should be
                connected to the input of all layers with indices in 
                list_of_destinations; skip_type specifies the type of
                connection: summing up if `skip_type=='sum'` and concatenating
                if `skip_type=='con'`

                CAUTIOUS: all elements in list_of_destinatins are assumed
                          to be larger than the (source) layer_index
        
            **feature_layer** : int (default: None)
                If given, is the index of the layer whose output will be 
                marked as the features extracted from the network; this 
                index should be given in terms of the order of layers in
                `layer_dict`
        
            **dropout** : list of two elements (default: None)
                If any layer should be dropped out during the training,
                this list contains the layers that need to be dropped out
                (first item) and the drop-out rate (second item).
        """

        self.x = x
        self.name = name
        self.batch_size = tf.shape(x)[0]  # to be used in conv2d_transpose
        self.layer_dict = layer_dict
        self.skips = skips
        self.input_shapes = []
        self.output_shapes = []

        if dropout:
            self.dropout_layers = dropout[0]
            self.dropout_rate = dropout[1]
        else:
            self.dropout_layers = []
            self.dropout_rate = 0.

        self.var_dict = {}
        self.layer_names = list(layer_dict.keys())

        self.probes = [{layer:[] for layer in probes[0]},
                       {layer:[] for layer in probes[1]}]

        sources_idx = [skips[i][0] for i in range(len(skips))]
        sources_output = []

        with tf.variable_scope(name, reuse=tf.AUTO_REUSE):

            self.global_step = tf.Variable(0, trainable=False, name='global_step')
            # setting the hyper-parameters
            self.set_hypers(**kwargs)
            self.learning_rate = self.lr_schedule(self.global_step)
            self.keep_prob = tf.placeholder(tf.float32, name='keep_prob')
            self.is_training = tf.placeholder(tf.bool, name='is_training')

            self.output = x
            for i, layer_name in enumerate(layer_dict):

                # before adding a layer check if this layer
                # is a destination layer, i.e. it has to
                # consider the output of a previous layer
                # in its input 
                combine_layer_outputs(self, 
                                      i, 
                                      skips, 
                                      sources_output)

                if layer_name in probes[0]:
                    self.probes[0].update({layer_name: self.output})

                layer = layer_dict[layer_name]
                if len(layer)==2:
                    layer += ['M']

                self.input_shapes += [[self.output.shape[i].value for i
                                       in range(len(self.output.shape))]]
                # layer[0]: layer type
                # layer[1]: layer specs
                # layer[2]: order of operations, default: 'M'
                self.add_layer(
                    layer_name, layer[0], layer[1], layer[2])

                self.output_shapes += [[self.output.shape[i].value for i
                                        in range(len(self.output.shape))]]

                # dropping out the output if the layer
                # is in the list of dropped-out layers
                if i in self.dropout_layers:
                    self.output = tf.nn.dropout(
                        self.output, self.keep_prob)

                if layer_name in probes[1]:
                    self.probes[1].update({layer_name: self.output})
                
                if i in sources_idx:
                    sources_output += [self.output]

                # set the output of the layer one before last as 
                # the features that the network will extract
                if i==feature_layer:
                    self.feature_layer = self.output
                
                # flatenning the output of the current layer
                # if it is 'conv' or 'pool' and the next
                # layer is 'fc' (hence needs flattenned input)
                if i<len(layer_dict)-1:
                    next_layer_type = layer_dict[
                        self.layer_names[i+1]][0]
                    if (layer[0]=='conv' or layer[0]=='pool') \
                       and next_layer_type=='fc':
                        # flattening the output
                        out_size = np.prod(
                            self.output.get_shape()[1:]).value
                        self.output = tf.reshape(
                            tf.transpose(self.output), 
                            [out_size, -1])
                        
            # creating the label node
            if len(self.output.shape)==2:
                # posterior
                posteriors = tf.nn.softmax(tf.transpose(self.output))
                self.posteriors = tf.transpose(posteriors, name='Posteriors')
                # prediction node
                self.prediction = tf.argmax(self.posteriors, 0, name='Prediction')
                c = self.output.get_shape()[0].value
                self.class_num = c
                self.y_ = tf.placeholder(tf.float32, 
                                         [c, None],
                                         name='labels')
                self.labels = tf.argmax(self.y_, axis=0)
            else:
                out_map_dim = [_.value for _ in self.output.shape[1:-1]]
                c = self.output.shape[-1].value
                if self.AU_4L or self.AU_4U:
                    if self.AU_4L:
                        # c is definitely even
                        c = int(c/2)
                    elif self.AU_4U:
                        c -= 1

                    if len(out_map_dim)==2:
                        self.clean_output = self.output[:,:,:,:c]
                        self.AU_vals = tf.nn.relu(self.output[:,:,:,c:])
                    elif len(out_map_dim)==3:
                        self.clean_output = self.output[:,:,:,:,:c]
                        self.AU_vals = tf.nn.relu(self.output[:,:,:,:,c:])

                    self.posteriors = tf.nn.softmax(self.clean_output)
                    # if AU values are to be used with labeled samples,
                    # corrupt the outputs with AU-dependent noise
                    if self.AU_4L:
                        corrupt_output_wAU_4L_FCN(self)
                    elif self.AU_4U:
                        if len(out_map_dim)==2:
                            self.AU_vals = self.AU_vals[:,:,:,0]
                        elif len(out_map_dim)==3:
                            self.AU_vals = self.AU_vals[:,:,:,:,0]

                else:
                    self.posteriors = tf.nn.softmax(self.output, name='posterior')
                self.y_ = tf.placeholder(tf.float32,[None,]+out_map_dim+[c,], name='one_hot_labels')
                self.labels = tf.argmax(self.y_, axis=-1, name='labels')
                self.prediction = tf.argmax(self.posteriors, axis=-1, name='prediction')
                self.class_num = c

    def add_layer(self, 
                  layer_name,
                  layer_type, 
                  layer_specs, 
                  layer_op_order):
        """Adding a layer to the graph
        
        Type of the next layer should also be given so that
        the appropriate output can be prepared
        
        :Parameters:
        
            **layer_specs** : list of three elements
                 specification list of the layer with
                 a format explaned in `__init__` as the
                 items of `layer_dict`
        
            **name** : string
        
            **next_layer_type** : string
                determining type of the next layer so that
                the output will be provided accordingly
        
            **last_layer** : binary flag
                determining whether the layer is the 
                last one; if it is `True` there won't be
                any activation at the output
        """
        
        with tf.variable_scope(layer_name, reuse=tf.AUTO_REUSE):
            for op in layer_op_order:

                if op=='M':
                    # main operation
                    if layer_type=='conv':
                        self.add_conv(layer_name, 
                                      layer_specs)
                    elif layer_type=='conv_transpose':
                        self.add_conv_transpose(layer_name, 
                                                layer_specs)
                    elif layer_type=='fc':
                        self.add_fc(layer_name, 
                                    layer_specs)
                    elif layer_type=='pool':
                        self.add_pool(layer_specs)
                    else:
                        raise ValueError(
                            "Layer's type should be either 'fc'" + 
                            ", 'conv', 'conv_transpose' or 'pool'.")

                elif op=='B':

                    # batch normalization
                    self.add_BN(layer_name, layer_specs)

                elif op=='A':
                    # activation
                    if self.activation=='ReLU':
                        self.output = tf.nn.relu(self.output)
                    elif self.activation=='tanh':
                        self.output = tf.nn.tanh(self.output)
                    else:
                        raise ValueError('The specified activation cannot be found.')

                else:
                    raise ValueError(
                        "Operations should be either 'M'" + 
                        ", 'B' or 'A'.")
                
    def add_conv(self, 
                 layer_name,
                 layer_specs):
        """Adding a convolutional layer to the graph given 
        the specifications

        The layer specifications should have the following
        order:
        - [num. of kernel, dim. of kernel, strides, padding]

        It should containt at least the first two elements,
        the other two have default values.

        CAUTIOUS: when giving default values be careful
        about the order of the parameters, e.g. you cannot
        use default values for strides, when padding is 
        assigned a value. The 3rd value will ALWAYS be
        assigned to strides 
        """

        # creating necessary TF variables
        kernel_num = layer_specs[0]
        kernel_dim = layer_specs[1]
        if len(layer_specs)==2:
            strides = [1]*len(kernel_dim)
            padding='SAME'
        elif len(layer_specs)==3:
            strides = layer_specs[2]
            padding='SAME'

        prev_depth = self.output.get_shape()[-1].value
        W = weight_variable('Weight',
                            kernel_dim + 
                            [prev_depth,
                             kernel_num],
                            self.regularizer,
                            self.custom_getter)
        b = bias_variable('Bias', [kernel_num], 
                          self.custom_getter)
        new_vars = [W,b]

        # there may have already been some variables
        # created for this layer (through BN)
        if layer_name in self.var_dict:
            self.var_dict[layer_name] += new_vars
        else:
            self.var_dict.update({layer_name: new_vars})


        # output of the layer
        if len(kernel_dim)==2:
            self.output = tf.nn.conv2d(
                self.output, W, 
                strides= [1,] + strides + [1,],
                padding=padding) + b

        elif len(kernel_dim)==3:
            self.output = tf.nn.conv3d(
                self.output, W,
                strides= [1,] + strides + [1,],
                padding = padding) + b
    
    def add_fc(self, 
               layer_name,
               layer_specs):
        """Adding a fully-connected layer with a given 
        specification to the graph
        """
        prev_depth = self.output.get_shape()[0].value
        new_vars = [weight_variable('Weight',
                                    [layer_specs[0], prev_depth],
                                    self.regularizer,
                                    self.custom_getter),
                    bias_variable('Bias', [layer_specs[0], 1],
                                  self.custom_getter)
                ]

        if layer_name in self.var_dict:
            self.var_dict[layer_name] += new_vars
        else:
            self.var_dict.update({layer_name: new_vars})

        # output of the layer
        self.output = tf.matmul(
            self.var_dict[layer_name][-2], 
            self.output) + self.var_dict[layer_name][-1]
            
    def add_pool(self, layer_specs):
        """Adding a (max-)pooling layer with given specifications
        
        * `layer_specs` : list
            A list of integers representing window size (=strides)

        NOTE: for now, the assumption here is that strides and
        window sizes are equal in all directions (because almost
        always we use max-pooling for reducing dimensioanlities)
        """

        w_sizes = layer_specs
        strides = layer_specs
        self.output = max_pool(self.output, 
                               w_sizes, 
                               strides)

    def add_BN(self, 
               layer_name,
               layer_specs):
        """Adding batch normalization layer

        Here, we used tf.contrib.layers.batch_norm which takes
        care of updating population mean and variance during the
        training phase by means of exponential moving averaging.
        Hence, we need an extra boolean placeholder for the model 
        that specifies when we are in the training phase and
        when we are in test.

        """

        # get the current scope
        scope = tf.get_variable_scope()
        # NOTE: We need to have a variable scope to create this
        # layer because we use the tf.contrib.layers.batch_norm
        # with `reuse=True`, which needs to be provided the
        # variable scope too

        # shape of the variables
        output_shape = self.output.shape
        if len(output_shape)==2:
            ax = [1]
            shape = [output_shape[0].value]
        else:
            ax = [0]
            #shape = [output_shape[i].value for i 
            #         in range(1,len(output_shape))]
            shape = [output_shape[-1].value]


        # creating the variables
        new_vars = [
            tf.get_variable('gamma', dtype=tf.float32,
                            initializer=tf.ones(shape),
                            custom_getter=self.custom_getter),
            tf.get_variable('beta', dtype=tf.float32,
                            initializer=tf.zeros(shape),
                            custom_getter=self.custom_getter),
            tf.get_variable('moving_mean', dtype=tf.float32,
                            initializer=tf.zeros(shape),
                            trainable=False),
            tf.get_variable('moving_variance', dtype=tf.float32,
                            initializer=tf.ones(shape),
                            trainable=False)]
        if layer_name in self.var_dict:
            self.var_dict[layer_name] += new_vars
        else:
            self.var_dict.update({layer_name: new_vars})
            
        if not(hasattr(self, 'BN_decay')):
            self.BN_decay = 0.999
        if not(hasattr(self, 'BN_epsilon')):
            self.BN_epsilon = 1e-3
        
        self.output = tf.contrib.layers.batch_norm(
            self.output,
            decay=self.BN_decay,
            center=True,
            scale=True,
            epsilon=self.BN_epsilon,
            is_training=self.is_training,
            reuse=True,
            scope=scope)

    def add_conv_transpose(self, 
                           layer_name,
                           layer_specs):
        """Adding a transpose convolutional layer
        
        Any transpose convolution is indeed the backward 
        direction of a convolution layer with some 
        ambiguity on the output's size (not fully clear)

        Number of elements in `layer_specs` of this
        layer should be EXACTLY three
        """
        kernel_num = layer_specs[0]
        kernel_dim = layer_specs[1]
        strides = layer_specs[2]

        # adding new variables
        prev_depth = self.output.get_shape()[-1].value
        W = weight_variable('Weight',
                            kernel_dim + 
                            [kernel_num, 
                             prev_depth],
                            self.regularizer,
                            self.custom_getter)
        b = bias_variable('Bias', [kernel_num],
                          self.custom_getter)
        new_vars = [W,b]

        # there may have already been some variables
        # created for this layer (through BN)
        if layer_name in self.var_dict:
            self.var_dict[layer_name] += new_vars
        else:
            self.var_dict.update({layer_name: new_vars})

        # output of the layer
        dim = len(self.x.shape) - 2
        input_size = [self.output.shape[i].value for
                      i in range(1,dim+1)]
        output_shape = [self.batch_size,] + \
                       [strides[i]*input_size[i] for i in range(dim)] +\
                       [kernel_num,]
        strides = [1,] + strides + [1,]

        # padding will stay `SAME` for now
        if dim==2:
            self.output = tf.nn.conv2d_transpose(
                self.output, W, output_shape, strides) + b
        elif dim==3:
            self.output = tf.nn.conv3d_transpose(
                self.output, W, output_shape, strides) + b

        # last line is adding zeros with the same size of the 
        # output (except batch size) only in order to
        # remove the size ambiguities that is created by 
        # tf.nn.conv2d_transpsoe
        if output_shape[-1]>1:
            self.output += tf.constant(0., shape=[1,]+output_shape[1:])
        else:
            output_shape[-1]=2
            self.output += tf.constant(0., shape=[1,]+output_shape[1:])
            if dim==2:
                self.output = self.output[:,:,:,:1]
            elif dim==3:
                self.output = self.output[:,:,:,:,:1]

    def set_hypers(self, **kwargs):

        kwargs.setdefault('activation', self.DEFAULT_HYPERS['activation'])
        kwargs.setdefault('custom_getter', self.DEFAULT_HYPERS['custom_getter'])
        kwargs.setdefault('BN_decay', self.DEFAULT_HYPERS['BN_decay'])
        kwargs.setdefault('BN_epsilon', self.DEFAULT_HYPERS['BN_epsilon'])
        # optimizer and loss
        kwargs.setdefault('loss_name', self.DEFAULT_HYPERS['loss_name'])
        kwargs.setdefault('optimizer_name', self.DEFAULT_HYPERS['optimizer_name'])
        kwargs.setdefault('lr_schedule', self.DEFAULT_HYPERS['lr_schedule'])
        if kwargs['optimizer_name']=='Adam':
            kwargs.setdefault('beta1', self.DEFAULT_HYPERS['beta1'])
            kwargs.setdefault('beta2', self.DEFAULT_HYPERS['beta2'])
        elif kwargs['optimizer_name']=='RMSProp':
            kwargs.setdefault('decay', self.DEFAULT_HYPERS['decay'])
            kwargs.setdefault('momentum', self.DEFAULT_HYPERS['momentum'])
            kwargs.setdefault('epsilon', self.DEFAULT_HYPERS['epsilon'])
        # weight regularization 
        kwargs.setdefault('regularizer', self.DEFAULT_HYPERS['regularizer'])
        # binary class weighting
        kwargs.setdefault('bin_class_weights', self.DEFAULT_HYPERS['bin_class_weights'])
        kwargs.setdefault('focal_gamma', self.DEFAULT_HYPERS['focal_gamma'])
        if kwargs['regularizer'] is not None:
            kwargs.setdefault('weight_decay', self.DEFAULT_HYPERS['weight_decay'])
        # aleatoric uncertainty
        kwargs.setdefault('AU_4L', self.DEFAULT_HYPERS['AU_4L'])
        kwargs.setdefault('AU_4U', self.DEFAULT_HYPERS['AU_4U'])
        kwargs.setdefault('AU_log_rel_coeff', self.DEFAULT_HYPERS['AU_log_rel_coeff'])
        if kwargs['AU_4L']:
            kwargs.setdefault('MC_T', self.DEFAULT_HYPERS['MC_T'])
        # consistency-based semi-supervised
        kwargs.setdefault('MT_SSL', self.DEFAULT_HYPERS['MT_SSL'])
        if kwargs['MT_SSL']:
            kwargs.setdefault('MT_ema_decay_schedule', self.DEFAULT_HYPERS['MT_ema_decay_schedule'])
            kwargs['MT_ema_decay'] = kwargs['MT_ema_decay_schedule'](self.global_step)
            kwargs.setdefault('Gaussian_noise_std', self.DEFAULT_HYPERS['Gaussian_noise_std'])
            kwargs.setdefault('rotation_angle', self.DEFAULT_HYPERS['rotation_angle'])
            kwargs.setdefault('output_perturbation_measure', self.DEFAULT_HYPERS['output_perturbation_measure'])
            kwargs.setdefault('max_cons_coeff', self.DEFAULT_HYPERS['max_cons_coeff'])
            kwargs.setdefault('rampup_length', self.DEFAULT_HYPERS['rampup_length'])

        self.__dict__.update(kwargs)

    def get_var_by_layer_and_op_name(self, layer_name, op_name):
        
        var_names = [var.name for var in self.var_dict[layer_name]]
        bin_op_indic = [op_name in var_name for var_name in var_names]
        op_loc_in_layer = np.where(np.array(bin_op_indic))[0][0]
        
        return self.var_dict[layer_name][op_loc_in_layer]

    def initialize(self, sess):
        """Initializing the model

        :Parameters:
        
            **sess** : Tensorflow session
                The active session in which the model is
                running
        """

        # initializing variables of this model only
        model_vars = [var for var in tf.global_variables()
                      if self.name in var.name] + [self.global_step]
        sess.run(tf.variables_initializer(model_vars))
                    

    def save_weights(self, file_path):
        """Saving only the parameter values of the 
        current model into a .h5 file
        
        The file will have as many groups as the number
        of layers in the model (which is equal to the
        number of keys in `self.var_dict`. Each group has
        two datasets, one for the weight W, and one for
        the bias b.
        """
        
        f = h5py.File(file_path, 'w')
        for layer_name, layer_vars in self.var_dict.items():
            L = f.create_group(layer_name)

            for var in layer_vars:
                var_name = var.name.split('/')[-1][:-2]
                # if self is a MT model, ignore last name, which
                # is ExponentialMovingAverage for all variables
                if 'Exponential' in var.name:
                    var_name = var.name.split('/')[-2]
                # last line, [:-2] accounts for ':0' in 
                # TF variables
                L.create_dataset(var_name, data=var.eval())

        # save the weights in branches too, if any
        if hasattr(self, 'branches'):
            for bname, branch in self.branches.items():
                B = f.create_group(bname)
                for layer_name, layer_vars in branch.var_dict.items():
                    # if the layer is from the main body,
                    # ignore it
                    if bname not in layer_vars[0].name:
                        continue

                    sub_B = B.create_group(layer_name)
                    for var in layer_vars:
                        var_name = var.name.split('/')[-1][:-2]
                        sub_B.create_dataset(var_name, data=var.eval())
            
        f.close()
        
    def load_weights(self, file_path, session):
        """Loading parameter values saved in a .h5 file
        into the tensorflow variables of the class object
        
        The groups in the .h5 file should match the layers
        in the model. Specifically, name of each group 
        needs to be the same as the name of the layers
        in `self.var_dict` (this is autmatically satisfied
        if the .h5 file is generated using self.save_model().
        """
        
        f = h5py.File(file_path)
        model_real_name = self.output.name.split('/')[0]

        for layer_name in list(self.var_dict):
            var_names = list(f[layer_name].keys())
            for var_name in var_names:
                var_value = np.array(f[layer_name][var_name])
                full_var_name = '/'.join([model_real_name,
                                          layer_name,
                                          var_name])
                tf_var = tf.get_collection(
                    tf.GraphKeys.GLOBAL_VARIABLES, 
                    scope=full_var_name)[0]
                session.run(tf_var.assign(var_value))
            
    def add_assign_ops(self):
        """Adding operations for assigning values to
        the nodes of the class's graph. This method
        is for creating repeatedly assigning values to
        the nodes after finalizing the graph. It should
        be called before `sess.graph.finalize()` to 
        create the operation nodes before finalizing.
        Then, the created operation nodes can be 
        performed without any need to create new nodes.
        
        Note that such repeated value assignment to the
        nodes are necessary for, say, querying iterations
        where after selecting each set of queries the model
        should be trained from scratch (or from the point
        that we saved the weights beforehand).
        
        This function, together with `self.perform_assign_ops`
        will be used instead of `self.load_weights` when 
        value assignment needs to be done repeatedly after
        finalizing the graph.

        The function defines a new attribute called `assign_dict`,
        a dictionary of layer names as the keys. Each item
        of `assign_dict` is itself a dictionary with keys
        equal to the variables names of that layer (from
        among `Weight`, `Bias`, `Scale` and `Offset`). Each
        item of this dictionary then has two elements: 

            - the assigning operation for the variable
              with the given name
            - the placeholder that will carry the value
              to be assigned to this variable

        In summary `assign_dict` has the following structure:

            {
             'layer_name_1': {'Weight': [<assign_op>,
                                         <placeholder>],
                              'Bias':   [<assign_op>,
                                         <placeholder>]}
                 :
             'branch_name_1': {layer_name_1: {'Weight': [<assign_op>,
                                                         <placeholder>],
                                              'Bias':   [<assign_op>,
                                                         <placeholder>]}
                               }
             }
        
        """

        self.assign_dict = {}

        # main body
        for layer_name, layer_vars in self.var_dict.items():

            layer_dict = {}
            for var in layer_vars:
                var_name = var.name.split('/')[-1][:-2]

                # value placeholder
                var_placeholder = tf.placeholder(var.dtype,
                                                 var.get_shape())
                # assigning ops
                assign_op = var.assign(var_placeholder)
                layer_dict.update({var_name:[assign_op,
                                             var_placeholder]})
            self.assign_dict.update({layer_name: layer_dict})

        # network branches, if any
        if hasattr(self, 'branches'):
            for bname, branch in self.branches.items():
                branch_dict = {}
                for layer_name, layer_vars in branch.var_dict.items():
                    # ignore those layers that are the main body too
                    if bname not in layer_vars[0].name:
                        continue
                    layer_dict = {}

                    for var in layer_vars:
                        var_name = var.name.split('/')[-1][:-2]
                        # value placeholder
                        var_placeholder = tf.placeholder(var.dtype,
                                                         var.get_shape())
                        # assigning ops
                        assign_op = var.assign(var_placeholder)

                        layer_dict[var_name] = [assign_op,
                                                var_placeholder]
                    branch_dict[layer_name] = layer_dict
                self.assign_dict[bname] = branch_dict
                

    def perform_assign_ops(self,file_path,sess):
        """Performing assignment operations that have
        been created by `self.add_assign_ops`.

        This function, together with `self.add_assign_ops`
        will be used instead of `self.load_weights` when 
        value assignment needs to be done repeatedly after
        finalizing the graph.
        """

        model_real_name = self.output.name.split('/')[0]
        f = h5py.File(file_path)

        # get a list of branch names, if any
        if hasattr(self, 'branches'):
            bnames = list(self.branches.keys())
        else:
            bnames = []

        # preparing the operation list to be performed
        # and the necessary `feed_dict`
        feed_dict={}
        ops_list = []
        for name in self.assign_dict:
            # this `name` could be a layer name, or a branch name
            if name in bnames:
                branch_name = name
                for layer_name in self.assign_dict[branch_name]:
                    var_names = list(f[branch_name][layer_name].keys())
                    for var_name in var_names:
                        var_value = np.array(f[branch_name][layer_name][var_name])
                        # adding the operation
                        ops_list += [self.assign_dict[branch_name][layer_name][var_name][0]]
                        # adding the corresponding value into feed_dict
                        feed_dict.update({
                            self.assign_dict[branch_name][layer_name][var_name][1]: 
                            var_value})
            else:
                layer_name = name
                var_names = list(f[layer_name].keys())
                for var_name in var_names:
                    var_value = np.array(f[layer_name][var_name])
                    # adding the operation
                    ops_list += [self.assign_dict[layer_name][var_name][0]]
                    # adding the corresponding value into feed_dict
                    feed_dict.update({
                        self.assign_dict[layer_name][var_name][1]: var_value})

        sess.run(ops_list, feed_dict=feed_dict)


    def get_optimizer(self):
        """Form the loss function and optimizer of the CNN graph
        
        :Parameters;
        
            **learning_rate** : positive float
                learning rate of the optimization, which is 
                proportional to the step length of the descent

            **layer_list** : list of strings
                list of names of those layers that are to be
                modified in the training step; if empty all
                the layers will be included. This list should
                be a subset of `self.var_dict.keys()`.
        """

        with tf.variable_scope(self.name, reuse=tf.AUTO_REUSE):
            if len(self.output.shape)==2:
                get_loss(self)
            else:
                get_FCN_loss(self)

            # adding regularization, if any
            if self.regularizer is not None:
                reg_term = tf.reduce_mean(
                    tf.get_collection(
                        tf.GraphKeys.REGULARIZATION_LOSSES))
                self.loss += self.weight_decay*reg_term

            get_optimizer(self)


    def perturb_input(self):

        self.perturbed_x = self.x

        # Gaussian noise
        if self.Gaussian_noise_std is not None:
            eps = tf.random_normal(tf.shape(self.perturbed_x), 
                                   stddev=self.Gaussian_noise_std)
            self.perturbed_x = tf.add(model.perturbed_x, eps)
        
        # rotation
        if self.rotation_angle is not None:
            self.perturbed_x = tf.contrib.image.rotate(
                self.perturbed_x, self.rotation_angle)
            
    def train(self,sess,
              global_step_limit,
              train_gen,
              metric_gens=[],
              eval_step=100,
              save_path=None):
        """ The argument `metric_gens` should be a list of
        two elements: (1) the set of metrics to be computed
        as the evaluation metrics, (2) the data generator to be used
        for computing the metrics
        """

        if len(metric_gens)>0:
            if not(isinstance(metric_gens[0], list)):
                   metric_gens = [metric_gens]
        for i in range(len(metric_gens)):
            valid_dict = {}
            for metric in metric_gens[i][0]:
                metric_path = os.path.join(save_path,'%s_%d.txt'%(metric, i))
                if os.path.exists(metric_path):
                    M = list(np.loadtxt(metric_path))
                else:
                    M = []
                valid_dict.update({metric: M})    
            setattr(self, 'valid_metrics_%d'%i, valid_dict)

        # training iterations
        while self.global_step.eval() < global_step_limit:

            batch_X, batch_Y = train_gen()

            # first, have an initial evaluation
            # (if a validation generator is given)
            if not(self.global_step.eval()%eval_step):
                for i in range(len(metric_gens)):
                    eval_metrics(self, sess, 
                                 metric_gens[i][1], 
                                 metric_gens[i][2],
                                 True,
                                 'valid_metrics_%d'%i)
                    if save_path is not None:
                        for metric in metric_gens[i][0]:
                            np.savetxt(os.path.join(save_path,'%s_%d.txt'%(metric, i)), 
                                       getattr(self, 'valid_metrics_%d'%i)[metric])
                               
                        if self.global_step.eval()>0:
                            self.save_weights(os.path.join(save_path, 'model_pars.h5'))
                            if hasattr(self, 'teacher'):
                                self.teacher.save_weights(os.path.join(save_path, 
                                                                       'teacher_pars.h5'))

                            if len(metric_gens[0])==4:
                                V =  self.valid_metrics_0[metric_gens[0][3]]
                                if np.all(V[-1] > V[:-1]):
                                    np.savetxt(os.path.join(save_path,'max_valid_iter.txt'), 
                                               [self.global_step.eval()])
                                    self.save_weights(os.path.join(save_path,
                                                                   'max_model_pars.h5'))
                                    if hasattr(self, 'teacher'):
                                        self.teacher.save_weights(os.path.join(save_path, 
                                                                               'max_teacher_pars.h5'))


            # --------------------------------------------- #
            # --------------------------------------------- #

            feed_dict = {self.x: batch_X,
                         self.y_: batch_Y,
                         self.keep_prob:1-self.dropout_rate,
                         self.is_training: True}
            # setting up extra feed-dict if a teacher exists
            if hasattr(self, 'teacher'):
                feed_dict.update({
                    self.teacher.keep_prob:1.-self.teacher.dropout_rate,
                    self.teacher.is_training: True})

            sess.run(self.train_step, feed_dict=feed_dict);

            if self.MT_SSL:
                # update the teacher
                sess.run(self.ema_apply)
            
        
    def get_gradients(self, grad_layers=[]):
        """Forming gradients of the log-posteriors
        """
        
        # collect all the trainable variabels
        self.grad_layers = grad_layers
        if len(grad_layers)==0:
            gpars = tf.trainable_variables()
        else:
            gpars = []
            for layer in grad_layers:
                gpars += self.var_dict[layer]
        
        self.grad_posts = {}
        c = self.output.get_shape()[0].value
        # in binary classification, get only
        # gradient of the first class

        for j in range(c):
            self.grad_posts.update(
                {str(j): tf.gradients(
                    tf.log(self.posteriors[j, 0]),
                    gpars, name='score_class_%d'% j)
             }
             )
    def count_parameters(self):
        cnt = 0
        for _,pars in self.var_dict.items():
            for par in pars:
                var_shape = par.shape
                cnt += np.prod([var_shape[i].value for
                                i in range(len(var_shape))])
        return cnt

    def get_par_placeholders(self):
        """Getting a set of placeholders with the same size
        and structure as `model.grads_vars`
        """
        if hasattr(self, 'par_placeholders'):
            print('The model already has parameter placeholders..')
            return

        self.par_placeholders = []
        for i in range(len(self.grads_vars)):
            self.par_placeholders += [
                tf.placeholder(self.grads_vars[i][1].dtype,
                               self.grads_vars[i][1].shape)]

    def update_BN_stats(self, sess, sample_gen):
        """Only update data statistics in BN while keeping
        everything else fixed

        `grnd_paths` won't be used here and is given just to
        avoid any modification of the batch-preparer that needs
        the ground-truth paths too
        """

        BN_updates = [par for par in sess.graph.get_collection(tf.GraphKeys.UPDATE_OPS)
                      if self.name in par.name]
    
        # resetting statistics (moving averages) in BN layers
        BN_pars = [self.var_dict[layer][i] for layer in list(self.var_dict.keys()) 
                   for i in range(len(self.var_dict[layer])) 
                   if 'moving' in self.var_dict[layer][i].name]

        #sess.run(tf.variables_initializer(BN_pars))

        # starting to estimate new batch statistics
        iters = 200
        for i in range(iters):
            batch_X,_ = sample_gen()
            feed_dict={self.x:batch_X, self.keep_prob:1., self.is_training:True}
            sess.run(BN_updates, feed_dict=feed_dict)

    def create_branch(self, layer_dict, probed_layer_name, branch_name, **kwargs):
        """
        Creating a branch at the input of the specified layer; hence the input
        of that layer has to be already probed to be accissble by the branch
        """

        assert probed_layer_name in self.probes[0], 'The input to layer '+\
            '{} has not been probed when creating the main model, hence '+\
            'cannot be accessed by branch constructor.'
    
        if not(hasattr(self, 'branches')):
            self.branches = {}
            self.branches_input_layer = {}
            
        self.branches_input_layer[branch_name] = probed_layer_name
        x = self.probes[0][probed_layer_name]
        with tf.variable_scope(self.name):
            branch = CNN(x, layer_dict, branch_name, [], None,
                         [self.dropout_layers, self.dropout_rate],
                         **kwargs)
            # extension of var_dict in the branch
            for layer_name in self.var_dict:
                if probed_layer_name==layer_name:
                    break
                branch.var_dict[layer_name] = self.var_dict[layer_name]

            self.branches[branch_name] = branch

    def get_optimizer_for_branches(self):

        for _,branch in self.branches.items():
            branch.get_optimizer()
            

def combine_layer_outputs(model,
                          layer_index,
                          skips,
                          sources_output):
    """Preparing the inputs to a given layer
    considering the skip connections that may
    indicate the layer needs inputs from the
    previous layers 

    NOTE: for now, this function only works for 
    combining 2D feature maps (not supporting
    1D feature vectors--this is mostly for 
    handling resizing issues)

    Assumption for case of 3D images:
    - - - - - - - - - - - - - - - -
    For 3D images, i.e. 5-dimensional batches,
    we assume that variables do not need to be resized
    in depth:

    output shape      = [?, zo, ho, wo, mo] 
    shape of a source = [?, zs, hs, ws, ms]
    
    assumption: zo=zs (equal depths)
    """

    sources_for_sink = []
    for j in range(len(skips)):
        if layer_index in skips[j][1]:
            sources_for_sink += [j]

    if len(sources_for_sink)==0:
        return
    else:
        # get the shape of source variables
        if len(model.output.shape)==4:
            shapes = [tuple([shape.value for shape in par.shape[1:3]])
                      for par in np.array(sources_output)[sources_for_sink]]
            ho = model.output.shape[1].value
            wo = model.output.shape[2].value
        else:
            shapes = [tuple([shape.value for shape in par.shape[2:4]])
                      for par in np.array(sources_output)[sources_for_sink]]
            ho = model.output.shape[2].value
            wo = model.output.shape[3].value
        h = shapes[0][0]
        w = shapes[0][1]

        assert len(set(shapes))==1, pdb.set_trace() 
        
        # now compare size of the output with the skipped
        # variables, and resize if needed
        if (h != ho) or (w != wo):
            print('The output is resized from (%d,%d)'%
                  (ho,wo)+' to (%d,%d)'%(h,w))
            is_added = model.output==sources_output[-1]
            if len(model.output.shape)==4:
                model.output = tf.image.resize_image_with_crop_or_pad(
                    model.output, h, w)
            else:
                # if 3D, do the resizes one-by-one for each depth
                model.output = tf.stack([tf.image.resize_image_with_crop_or_pad(
                    model.output[:,i,:,:,:], h, w) for i in range(model.output.shape[1].value)],
                                        axis=1)
            # if model.output has already been added
            # to sources_output (and if so, this can be done only
            # by the previous layer) change that too
            if is_added:
                sources_output[-1] = model.output

        for j in sources_for_sink:
            if skips[j][2]=='sum':
                model.output = tf.add(model.output,
                                sources_output[j])
            elif skips[j][2]=='con':
                model.output = concat_outputs(
                    model.output, sources_output[j])


def concat_outputs(curr_output, prev_output):
    """Concatenating output of a layer in the 
    network with the output of some previous
    layers
    """

    assert len(curr_output.shape)==len(prev_output.shape), \
        'The outputs to concatenate should have the same' + \
        ' dimensionalities.'

    d = len(curr_output.shape)
    if d==2:
        output = tf.concat((prev_output, curr_output),
                           axis=0)
    else:
        output = tf.concat((prev_output, curr_output),
                           axis=d-1)

    return output


def get_loss(model):
            
    if model.loss_name=='CE':
        # this CE only takes exclusive class labels

        model.labeled_loss_weights = tf.to_float(
            tf.not_equal(tf.reduce_sum(tf.transpose(model.y_), axis=-1), 0.)
        )

        if model.focal_gamma is not None:
            model.pt = tf.where(tf.equal(tf.to_float(model.labels), 1.),
                                model.posteriors[1,:],
                                model.posteriors[0,:])
            focal_weights = tf.pow(1.-model.pt, model.focal_gamma)
            model.vox_labeled_loss_weights = tf.multiply(
                focal_weights,
                model.labeled_loss_weights)

        if model.bin_class_weights is not None:
            class_zero = tf.multiply(
                tf.ones_like(model.labels, dtype=tf.float32),
                model.bin_class_weights[0])
            class_one = tf.multiply(
                tf.ones_like(model.labels, dtype=tf.float32),
                model.bin_class_weights[1])
            class_weights_tensor = tf.where(
                tf.equal(tf.to_float(model.labels), 1.),
                class_one,
                class_zero)
            model.labeled_loss_weights = tf.multiply(
                model.labeled_loss_weights,
                class_weights_tensor)

        if hasattr(model, 'input_vox_weights'):
            model.labeled_loss_weights = tf.multiply(
                model.labeled_loss_weights,
                model.input_weights)

        model.loss = tf.losses.sparse_softmax_cross_entropy(
            labels=model.labels, logits=tf.transpose(model.output),
            weights=model.labeled_loss_weights)

    elif model.loss_name=='CE_softclasses':
        # this CE accepts soft class assignments as the
        # target distribution too.
        # NOTE: there is still no way to assign weights to the
        # training samples with this loss function.

        model.loss = tf.reduce_mean(
            tf.nn.softmax_cross_entropy_with_logits(
                labels=tf.transpose(model.y_), 
                logits=tf.transpose(model.output)),
            name='CE_Loss')

    elif model.loss_name=='GCE':

        clipped_posts = tf.clip_by_value(model.posteriors,1e-4,1-1e-4)
        model.Lq = compute_Lq(clipped_posts, model.q)
        model.loss = tf.reduce_mean(model.y_*model.Lq, axis=0)


    else:
        print("WARNING: No loss with name {} is found.".format(
            model.loss_name))


def get_FCN_loss(model):
    
    # Loss 
    if model.loss_name=='CE':
        # vox_labeled_loss_weights will be the weights of
        # labeled samples in CE loss or its imbalanced variants 
        model.vox_labeled_loss_weights = tf.to_float(
            tf.not_equal(tf.reduce_sum(model.y_, axis=-1), 0.)
        )

        # if focal loss is being used with CE
        if model.focal_gamma is not None:
            if len(model.output.shape)==4:
                # model including 2D CNN layers
                model.pt = tf.where(tf.equal(tf.to_float(model.labels), 1.),
                                    model.posteriors[:,:,:,1],
                                    model.posteriors[:,:,:,0])
            elif len(model.output.shape)==5:
                # model including 3D CNN layers
                model.pt = tf.where(tf.equal(tf.to_float(model.labels), 1.),
                                    model.posteriors[:,:,:,:,1],
                                    model.posteriors[:,:,:,:,0])

            focal_weights = tf.pow(1.-model.pt, model.focal_gamma)
            model.vox_labeled_loss_weights = tf.multiply(
                focal_weights,
                model.vox_labeled_loss_weights)

        if model.bin_class_weights is not None:
            class_zero = tf.multiply(
                tf.ones_like(model.labels, dtype=tf.float32),
                model.bin_class_weights[0])
            class_one = tf.multiply(
                tf.ones_like(model.labels, dtype=tf.float32),
                model.bin_class_weights[1])
            class_weights_tensor = tf.where(
                tf.equal(tf.to_float(model.labels), 1.),
                class_one,
                class_zero)
            model.vox_labeled_loss_weights = tf.multiply(
                model.vox_labeled_loss_weights,
                class_weights_tensor)

        if hasattr(model, 'input_vox_weights'):
            model.vox_labeled_loss_weights = tf.multiply(
                model.vox_labeled_loss_weights,
                model.input_vox_weights)

        model.loss = tf.losses.sparse_softmax_cross_entropy(
            labels=model.labels, logits=model.output,
            weights=model.vox_labeled_loss_weights)

    if model.MT_SSL:

        model.labeled_loss = model.loss

        # perturbing the input
        if not(hasattr(model, 'perturbed_x')):
            model.perturb_input()

        # building the teacher...
        # set up the EMA operations and MT model too
        model.ema = tf.train.ExponentialMovingAverage(decay=model.MT_ema_decay)
        V = []
        for _,Vars in model.var_dict.items():
            for var in Vars:
                if 'moving' not in var.name:
                    V += [var]
        model.ema_apply = model.ema.apply(V)

        MT_x = model.perturbed_x
        def custom_getter(getter, name, *args, **kwargs):
            var = getter(name, *args, **kwargs)
            op_name = var.name.split('/')[-1].split(':')[0]
            layer_name = var.name.split('/')[-2]
            target_var = model.get_var_by_layer_and_op_name(layer_name, op_name)
            return model.ema.average(target_var)
        keywords = {'custom_getter': custom_getter,
                    'AU_4U': model.AU_4U,
                    'AU_4L': model.AU_4L}
        model.teacher = CNN(MT_x, model.layer_dict, model.name+'_teacher',
                            model.skips, dropout=[model.dropout_layers,
                                                  model.dropout_rate], **keywords)
        model.teacher.output = tf.stop_gradient(model.teacher.output)
        if len(model.teacher.output.shape)==2:
            get_loss(model.teacher)
        else:
            get_FCN_loss(model.teacher)

        # forming the consistency loss
        model.cons_loss = measure_output_perturbation(model)
        if model.AU_4U:
            # if AU should be used for unlabeled samples, include the
            # AU values inside the MT consistency terms
            # AU_vals = log(sigma(x)^2)
            model.cons_loss = tf.reduce_mean(tf.reduce_mean(
                tf.multiply(model.cons_loss,
                            tf.exp(-model.AU_vals))+0.5*model.AU_log_rel_coeff*model.AU_vals, 
                axis=[1,2]))
        else:
            model.cons_loss = tf.reduce_mean(tf.reduce_mean(
                model.cons_loss, axis=[1,2]))

        # consistency coefficient
        sigmoid_rampup_value = sigmoid_rampup(model.global_step,
                                              model.rampup_length)
        model.cons_coeff = tf.multiply(sigmoid_rampup_value,
                                       model.max_cons_coeff)

        # ... finally, here's the total loss
        model.loss = tf.add(model.loss, 
                            tf.multiply(model.cons_coeff, model.cons_loss))

def get_optimizer(model):
    """Creating an optimizer (if needed) together with
    training step for a given loss
    """

    # building the optimizer
    if not(hasattr(model, 'optmizer')):
        if model.optimizer_name=='SGD':
            model.optimizer = tf.train.GradientDescentOptimizer(
                model.learning_rate, name=model.optimizer_name)
        elif model.optimizer_name=='Adam':
            model.optimizer = tf.train.AdamOptimizer(
                model.learning_rate, 
                model.beta1, 
                model.beta2,
                name=model.optimizer_name)
        elif model.optimizer_name=='RMSProp':
            model.optimizer = tf.train.RMSPropOptimizer(
            model.learning_rate,
            model.decay,
            model.momentum,
            model.epsilon)

    # gradients-and-variables to be applied with
    # optimizer.apply_gradients
    grads_vars = model.optimizer.compute_gradients(
        model.loss, model.var_dict)
    model.grads_vars = [GV for GV in grads_vars if
                        GV[1] in tf.get_collection(
                                     tf.GraphKeys.TRAINABLE_VARIABLES)]

    """check if only certain layers are to be modified
    in training/fine-tuning"""
    if hasattr(model, 'train_mask'):
        new_grads_vars = []
        # only keep those gradient-variable pairs whose name
        # is part of the given training mask
        for _,grad_var in enumerate(model.grads_vars):
            if np.any([name in grad_var[1].name.split(':')[0] 
                       for name in model.train_mask]):
                new_grads_vars += [grad_var]
        model.grads_vars = new_grads_vars

    """ doing partial fine-tuning (PFT) if needed"""
    if hasattr(model, 'PFT_bflag'):
        if not(hasattr(model,'par_placeholders')):
            model.get_par_placeholders()
        
        for i in range(len(model.grads_vars)):
            model.grads_vars[i] = (tf.multiply(
                model.grads_vars[i][0],model.par_placeholders[i]),
                                   model.grads_vars[i][1])
 
    # finally, creating the train-step by applying the 
    # resulted gradients-and-variables, and considering
    # the dependency of the apply_gradient operation
    # to the moving average updates of BN (if any), which
    # are in tf.GraphKeys.UPDATE_OPS
    update_ops = tf.get_collection(tf.GraphKeys.UPDATE_OPS)
    with tf.control_dependencies(update_ops):
        model.train_step = model.optimizer.apply_gradients(
            model.grads_vars, global_step=model.global_step)


def sigmoid_rampup(global_step, rampup_length):
    """Function for ramping up (used for making a schedule
    for learning rate, consistency coefficient, etc)

    Directly copied from Mean-Teacher repository
        https://github.com/CuriousAI/mean-teacher/blob/
        master/tensorflow/mean_teacher/model.py
    """

    global_step = tf.to_float(global_step)
    rampup_length = tf.to_float(rampup_length)
    def ramp():
        phase = 1.0 - tf.maximum(0.0, global_step) / rampup_length
        return tf.exp(-5.0 * phase * phase)

    result = tf.cond(global_step < rampup_length, 
                     ramp, lambda: tf.constant(1.0))
    return tf.identity(result, name="sigmoid_rampup")

def sigmoid_rampdown(global_step, rampdown_length, training_length):
    """Function for ramping down (used for making a schedule
    for learning rate, consistency coefficient, etc)

    Directly copied from Mean-Teacher repository
        https://github.com/CuriousAI/mean-teacher/blob/
        master/tensorflow/mean_teacher/model.py
    """

    global_step = tf.to_float(global_step)
    rampdown_length = tf.to_float(rampdown_length)
    training_length = tf.to_float(training_length)
    def ramp():
        phase = 1.0 - tf.maximum(0.0, training_length - global_step) / rampdown_length
        return tf.exp(-12.5 * phase * phase)

    result = tf.cond(global_step >= training_length - rampdown_length,
                     ramp,
                     lambda: tf.constant(1.0))
    return tf.identity(result, name="sigmoid_rampdown")

def sigmoid_schedule(global_step, max_lr, rampup_length, 
                     rampdown_length, train_length):

    global_step = tf.to_float(global_step)
    max_lr = tf.to_float(max_lr)
    rampup_length = tf.to_float(rampup_length)
    rampdown_length = tf.to_float(rampdown_length)
    train_length = tf.to_float(train_length)

    rampup_val = sigmoid_rampup(global_step, rampup_length)
    rampdown_val = sigmoid_rampdown(global_step, 
                                    rampdown_length,
                                    train_length)
    result = rampup_val*rampdown_val*max_lr

    return tf.identity(result)


def exponential_decay(init_lr, global_step, decay_rate):

    global_step = tf.to_float(global_step)
    decay_rate = tf.to_float(decay_rate)
    init_lr = tf.to_float(init_lr)

    result = init_lr*tf.exp(-global_step*decay_rate)
    return tf.identity(result, name="exp_decay")


def compute_Lq(likes, q):

    assert q!=0, 'q cannot be equal to zero.'
    return (1.-tf.pow(likes,q)) / q

def measure_output_perturbation(model):
    """Computing divergence between the output class probability
    distribution of the model, and the pertubed version of it

    The divergence measure is specified by `model.output_perturbation_measure`,
    and the perturbation is obtained by perutrbing parameters of the 
    teacher model (which could be the EMA model) and the perturbing
    the input (e.g., by rotation and blurring). The latter will be 
    given through a placeholder `model.output_placeholder`
    """
    
    if model.output_perturbation_measure=='L2':
        div = tf.reduce_mean(
            tf.square(model.posteriors-model.teacher.posteriors),
            axis=3)
    elif model.output_perturbation_measure=='CE':
        if model.AU_4U:
            teacher_logits = model.teacher.clean_output
        else:
            teacher_logits = model.teacher.output

        div = tf.nn.softmax_cross_entropy_with_logits(
            labels=model.posteriors,
            logits=teacher_logits)

    return div

def corrupt_output_wAU_4L_FCN(model):
    """Corrputing outut with AU for labeled samples
    in FCN models 
    """

    # eps_t (for each t, the same eps_t for the whole batch)
    # t=1,...,T (=model.MC_T)
    noise_dist = tf.distributions.Laplace(0.,1.)
    eps = noise_dist.sample([model.MC_T])
    #eps = tf.random_normal([model.MC_T])
    model.output = 0.
    for t in range(model.MC_T):
        logits_t = tf.add(model.clean_output, 
                          tf.scalar_mul(eps[t], model.AU_vals))
        model.output += tf.nn.softmax(logits_t, dim=-1)

        model.output = tf.divide(model.MC_probs,model.MC_T) + 1e-7

def weight_variable(name, shape, reg, custom_getter=None):
    """Creating a kernel tensor 
    with specified shape
    
    Here, as for the initialization we use
    the strategy that He et al. (2015), 
    "Delving deep into rectifiers: Surpassing 
    human level..."such that the outputs have 
    unit (reasonably large)
    
    It consists of Gaussian initialization
    with zero-mean and a specific variance.
    """

    # using Eq (10) of He et al., assuming
    # ReLu activation, independence of 
    # elements of the weight tensors, 
    # and independence between weights and
    # input tensors
    if len(shape)>2:
        # conv. layer
        # shape : [kernel dim] (length = 2 or 3)
        #         + [input_channels, output_channels]
        n = np.prod(shape[:-1])
        std = np.sqrt(2/n)
    else:
        # fc layer
        n = shape[1]
        std = np.sqrt(2/n)
    
    initial = tf.random_normal(
        shape, mean=0., stddev=std)
    
    return tf.get_variable(name, 
                           initializer=initial,
                           regularizer=reg,
                           custom_getter=custom_getter)

def bias_variable(name, shape, custom_getter=None):
    """Creating a bias term with specified shape
    """

    initial = tf.constant(0., shape=shape)
    return tf.get_variable(name, 
                           initializer=initial,
                           custom_getter=custom_getter)

    
def max_pool(x, w_sizes, strides, dim=None):
    """Dimensionality of the max-pooling here will be primarily
    determined by `w_sizes` and `strides`. `dim` will be used only when 
    both `w_sizes` and `strides` are scalars.
    """

    if isinstance(w_sizes, int) and isinstance(strides, int):
        if dim is None:
            raise ValueError('Ambiguity in max-pooling dimension.')

    if dim is None:
        dim = len(x.shape) - 2

    # Window search
    if isinstance(w_sizes, int):
        ksize = [1,] + [w_sizes]*dim + [1,]
    elif isinstance(w_sizes, list):
        if len(w_sizes)!=4 and len(w_sizes)!=5:
            # len(w_sizes) = 2 or 3
            ksize = [1,] + w_sizes + [1,]
        elif len(w_sizes)==4 or len(w_sizes)==5:
            # len(w_sizes) = 2 or 3
            ksize = w_sizes
        else:
            raise ValueError('Wrong dimension for window size in a max-pool.')
    else:
        raise ValueError('Window size in max-pool should be either an integer or list.')

    # Strides
    if isinstance(strides, int):
        strides = [1,] + [strides]*dim + [1,]
    elif isinstance(strides, list):
        if len(strides)!=4 and len(strides)!=5:
            strides = [1,] + strides + [1,]
    else:
        raise ValueError('Strides in max-pool should be either an integer or list.')

    if dim==2:
        return tf.nn.max_pool(
            x, ksize=ksize,
            strides=strides, 
            padding='SAME')
    elif dim==3:
        return tf.nn.max_pool3d(
            x, ksize=ksize,
            strides=strides,
            padding='SAME')
    

def replicate_model(model, 
                    name_extension='_2',
                    same_input=False):
    """Replicating architecture of a model including its 
    branches. This function mainly replicates the structure 
    of the input model, and does not copy the opimitzation
    settings

    If `same_input` flag is set to `True`, the replicated
    model will have the same input as the main model.
    """

    # main body of the model
    new_name = model.name + name_extension
    suffix = 3
    while exists_model_name(new_name):
        new_name = new_name[:-2] + '_{}'.format(suffix)
        suffix += 1

    layer_dict = model.layer_dict
    if same_input:
        x = model.x
    else:
        x = tf.placeholder(tf.float32, model.x.shape)
    # put probes in the proper places in case
    # there are branches in the source model
    probes = [[], []]
    if hasattr(model, 'branches_input_layer'):
        for _,input_layer in model.branches_input_layer.items():
            probes[0] += [input_layer]
    
    rep_model = CNN(x, layer_dict, new_name, model.skips,
                    None, None, probes)

    # branches, if any
    if hasattr(model, 'branches'):
        for bname, branch in model.branches.items():
            b_layer_dict = branch.layer_dict
            # take only those layers from the branch
            b_layer_dict = {
                s: branch.layer_dict[s] 
                for s in list(branch.var_dict.keys())
                if bname in branch.var_dict[s][0].name}
            # location of the branch (name of the layer
            # whose input will be the input of the branch)
            branch_input_layer = model.branches_input_layer[bname]
            rep_model.create_branch(b_layer_dict,
                                    branch_input_layer,
                                    bname)

    return rep_model


def exists_model_name(name):
    """Check if a model's name already exists
    """
    return np.any([name+'/' in v.name for v in 
                   tf.trainable_variables()])
