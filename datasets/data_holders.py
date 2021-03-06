import nibabel as nib
import numpy as np
import nrrd
import pdb

from .path_loader import extract_ISBI2015_MSLesion_data_path
from .utils import gen_minibatch_labeled_unlabeled_inds, \
    gen_minibatch_materials, global2local_inds, prepare_batch_BrVol

class regular(object):

    def __init__(self,
                 img_addrs,
                 mask_addrs,
                 data_reader,
                 rnd_seed, 
                 LUV_inds_or_sizes,
                 class_labels):
        """
        * LUV = Labeled + Unlabeled + Validation
          This list characterizes sample indices for these three
          subsets. It can be either a list of arrays which explicitly
          specifies indices of each subset, or a list of integers that
          only includes their sizes. 

              inds:   LUV_inds_or_sizes = [L_inds, U_inds, V_inds]
                                           ------  ------  ------
                                            array   array   array

              sizes:  LUV_inds_or_sizes = [L_size, U_size, V_size]
                                           ------  ------  ------
                                             int     int    int

          When this input has arrays of indices, the input "rnd_seed"
          won't be used in the constructor at all.

          All the remaining indices will be assigned to test
          data set, i.e. 
              test_inds = {1..n} - {inds of L} - {inds of U} - {inds of V}   
        """

        self.class_labels = class_labels
        self.C = len(self.class_labels)
        self.seed = rnd_seed
        self.reader = data_reader
        self.mask_reader = lambda x: self.read_mask(x)
        self.img_addrs = img_addrs
        self.mask_addrs = mask_addrs
        self.mods = list(img_addrs.keys())
        self.combined_paths = [[img_addrs[mod][i] for mod in self.mods] 
                               for i in range(len(img_addrs[self.mods[0]]))]
        n = len(self.combined_paths)

        if isinstance(LUV_inds_or_sizes[0], np.ndarray):
            self.labeled_inds   = LUV_inds_or_sizes[0]
            self.unlabeled_inds = LUV_inds_or_sizes[1]
            self.valid_inds     = LUV_inds_or_sizes[2]
            self.train_inds = np.concatenate((self.labeled_inds,
                                              self.unlabeled_inds))
        else:
            rand_inds = np.random.RandomState(seed=rnd_seed).permutation(n)
            self.labeled_inds = rand_inds[:LUV_inds_or_sizes[0]]
            self.unlabeled_inds = rand_inds[LUV_inds_or_sizes[0] : 
                                            LUV_inds_or_sizes[0]+LUV_inds_or_sizes[1]]
            self.train_inds = np.concatenate((self.labeled_inds, 
                                              self.unlabeled_inds))
            ntrain = len(self.train_inds)
            self.valid_inds = rand_inds[ntrain : ntrain+LUV_inds_or_sizes[2]]
        

        self.L_indic = np.array([1]*len(self.labeled_inds) + \
                                [0]*len(self.unlabeled_inds))

        self.test_inds = np.array(list(set(np.arange(n)) - 
                                       set(self.train_inds) - 
                                       set(self.valid_inds)))
        
        self.tr_img_paths = [self.combined_paths[i] for i in self.train_inds]
        self.tr_mask_paths = [self.mask_addrs[i] for i in self.train_inds]
        self.val_img_paths = [self.combined_paths[i] for i in self.valid_inds]
        self.val_mask_paths = [self.mask_addrs[i] for i in self.valid_inds]
        self.test_img_paths = [self.combined_paths[i] for i in self.test_inds]
        self.test_mask_paths = [self.mask_addrs[i] for i in self.test_inds]

    def load_images(self):
        """Loading images of the training and validation
        partitions into memory
        """

        ntrain = len(self.tr_img_paths)
        self.tr_imgs  = [[] for i in range(ntrain)]
        self.tr_masks = [[] for i in range(ntrain)]
        for i in range(len(self.tr_img_paths)):
            for j in range(len(self.mods)):
                img = self.reader(self.tr_img_paths[i][j])
                self.tr_imgs[i] += [img]
            if self.tr_mask_paths[i]=='NA':
                # if a training sample does not have any mask path
                # put an all-zero mask in its place, just to keep
                # everything consistent, otherwise it will never be used
                # as this sample should be unlabeled 
                self.tr_masks[i] = np.zeros(img.shape)
            else:
                self.tr_masks[i] = self.mask_reader(self.tr_mask_paths[i])

        self.val_imgs  = [[] for i in range(len(self.valid_inds))]
        self.val_masks = [[] for i in range(len(self.valid_inds))]
        for i,_ in enumerate(self.valid_inds):
            for j in range(len(self.mods)):
                img = self.reader(self.val_img_paths[i][j])
                self.val_imgs[i] += [img]
            self.val_masks[i] = self.mask_reader(self.val_mask_paths[i])

    def read_mask(self, path):
        """This is useful when the masks have values other 
        than 1,...,c, so they have to be mapped appropriately
        """

        # first, read the original file
        orig_mask = self.reader(path)
        if np.any(self.class_labels != np.arange(self.C)):
            mask = np.zeros(orig_mask.shape)
            for c, label in enumerate(self.class_labels):
                mask[orig_mask==label] = c
            return mask

        else:
            return orig_mask

    def create_train_valid_gens(self, 
                                batch_size, 
                                img_shape,
                                valid_mode='random',
                                n_labeled_train=None):
        """Creating sample generator from training and validation
        data sets. 

        It is assumed that images are loaed through load_images().
        """

        # training
        if len(self.tr_masks)>0:
            self.train_n_slices = [self.tr_masks[i].shape[2] 
                                   for i in range(len(self.tr_masks))]
            self.slices_L_indic = np.concatenate(
                [np.ones(self.train_n_slices[i])*self.L_indic[i] 
                 for i in range(len(self.tr_masks))])
            # index generator
            train_generator = gen_minibatch_labeled_unlabeled_inds(
                self.slices_L_indic, batch_size, n_labeled_train)
            train_gen_slices = lambda: self.generate_images(
                img_shape, train_generator, 'training')

            self.train_gen_fn = train_gen_slices

        # validation
        if len(self.val_masks)>0:
            if valid_mode == 'random':
                valid_gen_inds = gen_minibatch_labeled_unlabeled_inds(
                    np.ones(len(self.val_imgs)), batch_size)
                valid_gen = lambda: self.valid_generator(
                    valid_gen_inds, img_shape, 'uniform')
            elif valid_mode == 'full':
                # just follow what has been done above for training
                self.valid_n_slices = [self.val_masks[i].shape[2] 
                                       for i in range(len(self.val_masks))]
                valid_slices_L_indic = np.concatenate(
                    [np.ones(self.valid_n_slices[i]) for i 
                     in range(len(self.valid_n_slices))])
                self.valid_generator = gen_minibatch_labeled_unlabeled_inds(
                    valid_slices_L_indic, batch_size, None)
                valid_gen = lambda: self.generate_images(
                    img_shape, self.valid_generator, 'validation')

            self.valid_gen_fn = valid_gen

    def generate_images(self,
                        img_shape, 
                        inds_generator,
                        mode='training'):

        inds = np.concatenate(next(inds_generator))
        if mode=='training':
            # extracting slice indices from the generated indices
            img_slice_inds = global2local_inds(inds, self.train_n_slices)
            img_inds = np.concatenate([
                np.ones(len(img_slice_inds[i]),dtype=int)*i 
                for i in range(len(img_slice_inds))])
            img_slice_inds = np.concatenate(img_slice_inds)
            imgs = [self.tr_imgs[int(i)] for i in img_inds]
            masks = [self.tr_masks[int(i)] for i in img_inds]
            if np.any(self.L_indic==0):
                inds_L_indic = np.ones(len(img_inds))
                inds_L_indic[self.L_indic[img_inds]==0] = 0
            else:
                inds_L_indic=None

        elif mode=='validation':
            img_slice_inds = global2local_inds(inds, self.valid_n_slices)
            img_inds = np.concatenate([
                np.ones(len(img_slice_inds[i]),dtype=int)*i 
                for i in range(len(img_slice_inds))])
            img_slice_inds = np.concatenate(img_slice_inds)
            imgs = [self.val_imgs[int(i)] for i in img_inds]
            masks = [self.val_masks[int(i)] for i in img_inds]
            inds_L_indic=None

        return prepare_batch_BrVol(
            imgs, masks, img_shape, self.C, img_slice_inds, inds_L_indic)


    def valid_generator(self, generator, 
                        img_shape,
                        slice_choice='uniform'):
    
        if hasattr(self, 'val_imgs'):
            (img_paths_or_mats, 
             mask_paths_or_mats) = gen_minibatch_materials(
                 generator, 
                 self.val_imgs, 
                 self.val_masks)
        else:
            (img_paths_or_mats,
             mask_paths_or_mats) = gen_minibatch_materials(
                 generator, 
                 self.val_img_paths, 
                 self.val_mask_paths)

        return prepare_batch_BrVol(img_paths_or_mats, 
                                   mask_paths_or_mats, 
                                   img_shape, 
                                   self.C, slice_choice)

    def combine_with_other_data(self, dat_2):

        assert self.mods==dat_2.mods, 'The combining data sets should have '+\
            'the same image modalities.'

        # combining everything
        for mod in self.mods:
            self.img_addrs[mod] += dat_2.img_addrs[mod]
        self.mask_addrs += dat_2.mask_addrs
        self.L_indic = np.concatenate((self.L_indic, dat_2.L_indic))
        # .. including the combined_paths
        self.train_inds = np.concatenate((self.train_inds,
                                         dat_2.train_inds+len(self.combined_paths)))
        self.valid_inds = np.concatenate((self.valid_inds,
                                         dat_2.valid_inds+len(self.combined_paths)))
        self.test_inds = np.concatenate((self.test_inds,
                                         dat_2.test_inds+len(self.combined_paths)))
        self.combined_paths += dat_2.combined_paths
        
        self.tr_img_paths = self.tr_img_paths + dat_2.tr_img_paths
        self.tr_mask_paths = self.tr_mask_paths + dat_2.tr_mask_paths
        self.val_img_paths = self.val_img_paths + dat_2.val_img_paths
        self.val_mask_paths = self.val_mask_paths + dat_2.val_mask_paths
        self.test_img_paths = self.test_img_paths + dat_2.test_img_paths
        self.test_mask_paths = self.test_mask_paths + dat_2.test_mask_paths

        # .. in case images are loaded
        if hasattr(self, 'tr_imgs') and hasattr(dat_2, 'tr_imgs'):
            self.tr_imgs = self.tr_imgs + dat_2.tr_imgs
            self.tr_masks = self.tr_masks + dat_2.tr_masks
            self.val_imgs = self.val_imgs + dat_2.val_imgs
            self.val_masks = self.val_masks + dat_2.val_masks


class D3(regular):
    # same constructor as `regular`
    
    def create_train_valid_gens(self, 
                                batch_size, 
                                img_shape,
                                valid_mode='random',
                                n_labeled_train=None):
        """Creating sample generator from training and validation
        data sets. 

        It is assumed that images are loaed through load_images().

        NOTE: Although, `img_shape` has the format `[h,w,z,m]`, where
        `h=height`, `w=width`, `z=depth` and `m=#modelities`, when
        it is used with a CNN class, the generators should generate
        batches in format `[b,z,h,w,m]` (with `b=batch size`).
        """

        assert len(img_shape)==3, 'Three values are needed for img_shape in this class'
        assert img_shape[0]%2==img_shape[1]%2, 'Height and Width sizes'+\
            'in img_shape should be odd.'

        # NOTE: the reason that we do not push for odd depth is that in our U-net-style  
        # 3D FCN model (Tiramisu), we can only work with depths that are 
        # multiplications of 64

        # training
        if len(self.tr_masks)>0:
            z = img_shape[2]
            z_rad = int(z/2)
            # we should ignore 2*z_rad slices in total 
            # (indices of valid slices: z_rad:-z_rad (Z-z_rad-1) )
            self.train_n_slices = [self.tr_masks[i].shape[2]-2*z_rad
                                   for i in range(len(self.tr_masks))]
            self.slices_L_indic = np.concatenate(
                [np.ones(self.train_n_slices[i])*self.L_indic[i] 
                 for i in range(len(self.tr_masks))])
            # index generator
            train_generator = gen_minibatch_labeled_unlabeled_inds(
                self.slices_L_indic, batch_size, n_labeled_train)
            # patch generator
            train_gen_slices = lambda: self.generate_images(
                img_shape, train_generator, 'training')

            self.train_gen_fn = train_gen_slices


    def generate_images(self,
                        img_shape, 
                        inds_generator,
                        mode='training'):
        """Loading a patch (slices) based on indices generated
        by a given index-generator
        """

        # generate indices
        z_rad = int(img_shape[2]/2)
        inds = np.concatenate(next(inds_generator))

        if mode=='training':
            # extracting slice indices from the generated indices
            img_slice_inds = global2local_inds(inds, self.train_n_slices)
            img_inds = np.concatenate([
                np.ones(len(img_slice_inds[i]),dtype=int)*i 
                for i in range(len(img_slice_inds))])
            # adjusting the indices by adding z/2 
            # (we already excluded z/2 from beginning and z/2 from
            #  the end of the indices)
            img_slice_inds = np.concatenate(img_slice_inds) + z_rad
            imgs = [self.tr_imgs[int(i)] for i in img_inds]
            masks = [self.tr_masks[int(i)] for i in img_inds]
            if np.any(self.L_indic==0):
                inds_L_indic = np.ones(len(img_inds))
                inds_L_indic[self.L_indic[img_inds]==0] = 0
            else:
                inds_L_indic=None

        elif mode=='validation':
            img_slice_inds = global2local_inds(inds, self.valid_n_slices)
            img_inds = np.concatenate([
                np.ones(len(img_slice_inds[i]),dtype=int)*i 
                for i in range(len(img_slice_inds))])
            img_slice_inds = np.concatenate(img_slice_inds)
            imgs = [self.val_imgs[int(i)] for i in img_inds]
            masks = [self.val_masks[int(i)] for i in img_inds]
            inds_L_indic=None

        return prepare_batch_BrVol(
            imgs, masks, img_shape, self.C, img_slice_inds, inds_L_indic)


def get_dat_for_FT(dat,slice_img_inds, 
                      keep_unlabeled=False):
    """The slice indices in `slice_img_inds` is a list
    of n arrays, where n is the size of `dat.train_inds`.
    The i-th array contains slice indices of the i-th
    image in `dat.tr_imgs[i+labeled_size]`. These slices are the selected
    queries, which will be added to the labeled part of
    the new data with the true labels (simulating the expert
    by the available ground truth segmentations that are
    already accessible for the images).

    """

    labeled_size = int(np.sum(dat.L_indic))
    assert len(slice_img_inds)==len(dat.tr_imgs[labeled_size:]), \
        "The list of queried slices and list of unlabeled "+\
        "images should have the same length."

    # although we don't care about the indices in the new data
    LUV_inds = [dat.labeled_inds, dat.unlabeled_inds ,dat.valid_inds]
    new_dat = regular(dat.img_addrs ,dat.mask_addrs, dat.reader,
                      None, LUV_inds, dat.class_labels)
    
    # modifying the data object
    new_labeled_imgs = dat.tr_imgs[:labeled_size]
    new_labeled_masks = dat.tr_masks[:labeled_size]
    new_unlabeled_imgs = []
    new_unlabeled_masks = []
    for i in range(len(slice_img_inds)):
        if len(slice_img_inds[i])>0:
            new_imgs = []
            for j in range(len(dat.mods)):
                new_imgs += [dat.tr_imgs[i+labeled_size][j][:,:,slice_img_inds[i]]]
            new_labeled_imgs += [new_imgs]
            new_labeled_masks += [dat.tr_masks[i+labeled_size][:,:,slice_img_inds[i]]]

            # add the unlabeled slices too, if necessary
            if keep_unlabeled:
                z = dat.tr_masks[i+labeled_size].shape[2]
                unlabeled_inds = np.delete(np.arange(z), slice_img_inds[i])
                new_imgs = []
                for j in range(len(dat.mods)):
                    new_imgs += [dat.tr_imgs[i+labeled_size][j][:,:,unlabeled_inds]]
                new_unlabeled_imgs += [new_imgs]
                new_unlabeled_masks += [dat.tr_masks[i+labeled_size][:,:,unlabeled_inds]]

    new_dat.tr_imgs = new_labeled_imgs + new_unlabeled_imgs
    new_dat.tr_masks = new_labeled_masks + new_unlabeled_masks

    new_dat.L_indic = np.concatenate((np.ones(len(new_labeled_masks)),
                                      np.zeros(len(new_unlabeled_masks))))

    new_dat.val_imgs = dat.val_imgs
    new_dat.val_masks = dat.val_masks

    return new_dat
