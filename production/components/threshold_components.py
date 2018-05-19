#! /usr/bin/python

import os
import argparse
import json
import time
import subprocess

import numpy as np
import z5py
import vigra
import nifty
import luigi
from concurrent import futures


# TODO more clean up (job config files)
# TODO computation with rois
class ThresholdTask(luigi.Task):
    """
    Run all thresholding tasks
    """

    # path to the n5 file and keys
    path = luigi.Parameter()
    aff_key = luigi.Parameter()
    mask_key = luigi.Parameter()
    out_key = luigi.Parameter()
    # maximal number of jobs that will be run in parallel
    max_jobs = luigi.IntParameter()
    # path to the configuration
    # TODO allow individual paths for individual blocks
    config_path = luigi.Parameter()
    tmp_folder = luigi.Parameter()
    # FIXME default does not work; this still needs to be specified
    time_estimate = luigi.IntParameter(default=10)
    run_local = luigi.BoolParameter(default=False)
    # TODO optional parameter to just run a subset of blocks

    def _submit_job(self, job_id):
        script_path = os.path.join(self.tmp_folder, 'threshold_components.py')
        assert os.path.exists(script_path)
        config_path = os.path.join(self.tmp_folder, 'threshold_config_job%i.json' % job_id)
        command = '%s %s %s %s %s %i %s %s' % (script_path, self.path, self.aff_key, self.mask_key, self.out_key,
                                               job_id, config_path, self.tmp_folder)
        log_file = os.path.join(self.tmp_folder, 'logs', 'log_threshold_block_%i' % job_id)
        err_file = os.path.join(self.tmp_folder, 'error_logs', 'err_threshold_block_%i.err' % job_id)
        bsub_command = 'bsub -J threshold_block_%i -We %i -o %s -e %s \'%s\'' % (job_id,
                                                                                 self.time_estimate,
                                                                                 log_file, err_file, command)
        if self.run_local:
            subprocess.call([command], shell=True)
        else:
            subprocess.call([bsub_command], shell=True)

    # TODO allow different configs for different blocks
    def _prepare_jobs(self, n_jobs, n_blocks, config):
        block_list = list(range(n_blocks))
        for job_id in range(n_jobs):
            block_jobs = block_list[job_id::n_jobs]
            job_config = {'config': config,
                          'block_list': block_jobs}
            config_path = os.path.join(self.tmp_folder, 'threshold_config_job%i.json' % job_id)
            with open(config_path, 'w') as f:
                json.dump(job_config, f)

    def _collect_outputs(self, n_blocks):
        times = []
        n_components = []
        processed_blocks = []
        for block_id in range(n_blocks):
            res_file = os.path.join(self.tmp_folder, 'threshold_result_block%i.json' % block_id)
            try:
                with open(res_file) as f:
                    res = json.load(f)
                    times.append(res['t'])
                    n_components.append(res['n_components'])
                processed_blocks.append(block_id)
                os.remove(res_file)
            except Exception:
                continue
        return processed_blocks, n_components, times

    def run(self):
        from .. import util

        # make the tmpdir
        try:
            os.mkdir(self.tmp_folder)
        except OSError:
            pass

        # copy the script to the temp folder and replace the shebang
        file_dir = os.path.dirname(os.path.abspath(__file__))
        util.copy_and_replace(os.path.join(file_dir, 'threshold_components.py'),
                              os.path.join(self.tmp_folder, 'threshold_components.py'))

        with open(self.config_path) as f:
            config = json.load(f)
            block_shape = config['block_shape']
            chunks = tuple(config['chunks'])
            # TODO support computation with roi
            if 'roi' in config:
                have_roi = True

        # find the shape and number of blocks
        f = z5py.File(self.path)
        ds = f[self.mask_key]
        shape = ds.shape
        blocking = nifty.tools.blocking([0, 0, 0], shape, block_shape)
        n_blocks = blocking.numberOfBlocks

        # make the output dataset
        f.require_dataset(self.out_key, shape=shape,
                          chunks=chunks, dtype='uint64', compression='gzip')

        # find the actual number of jobs and prepare job configs
        n_jobs = min(n_blocks, self.max_jobs)
        self._prepare_jobs(n_jobs, n_blocks, config)

        # submit the jobs
        # TODO would be better to wrap this into a process pool, but
        # it will be quite a pain to make everything pickleable
        if self.run_local:
            # this only works in python 3 ?!
            with futures.ProcessPoolExecutor(n_jobs) as tp:
                tasks = [tp.submit(self._submit_job, job_id)
                         for job_id in range(n_jobs)]
                [t.result() for t in tasks]
        else:
            for job_id in range(n_jobs):
                self._submit_job(job_id)

        # wait till all jobs are finished
        if not self.run_local:
            util.wait_for_jobs('papec')

        # check the job outputs
        processed_blocks, n_components, times = self._collect_outputs(n_blocks)
        assert len(processed_blocks) == len(n_components) == len(times)
        success = len(processed_blocks) == n_blocks

        # write output file if we succeed, otherwise write partial
        # success to different file and raise exception
        if success:
            out = self.output()
            # TODO does 'out' support with block?
            fres = out.open('w')
            json.dump({'n_components': n_components,
                       'times': times}, fres)
            fres.close()
        else:
            log_path = os.path.join(self.tmp_folder, 'threshold_partial.json')
            with open(log_path, 'w') as out:
                json.dump({'n_components': n_components,
                           'times': times,
                           'processed_blocks': processed_blocks}, out)
            raise RuntimeError("ThresholdTask failed, %i / %i blocks processed, serialized partial results to %s" % (len(processed_blocks),
                                                                                                                     n_blocks,
                                                                                                                     log_path))

    def output(self):
        return luigi.LocalTarget(os.path.join(self.tmp_folder, 'threshold.log'))


def threshold_blocks(path, aff_key, mask_key, out_key,
                     job_id, config_path, tmp_folder):
    """
    Run threshold on affinities for single block.
    """
    #

    # load datasets
    f5 = z5py.File(path)
    ds_affs = f5[aff_key]
    ds_mask = f5[mask_key]
    ds_out = f5[out_key]
    shape = ds_out.shape

    # load the configuration
    with open(config_path) as f:
        input_config = json.load(f)
        config = input_config['config']
        boundary_threshold = config['boundary_threshold']
        block_shape = config['block_shape']
        aff_slices = config['aff_slices']
        aff_slices = [(slice(sl[0], sl[1]),) for sl in aff_slices]
        invert_channels = config['invert_channels']
        assert len(aff_slices) == len(invert_channels)
        block_ids = input_config['block_list']

    for block_id in block_ids:
        print("Processing block", block_id)
        t0 = time.time()
        res_file = os.path.join(tmp_folder, 'threshold_result_block%i.json' % block_id)
        # get block bounding box
        blocking = nifty.tools.blocking([0, 0, 0],
                                        list(shape),
                                        list(block_shape))
        block = blocking.getBlock(block_id)
        bb = tuple(slice(beg, end)
                   for beg, end in zip(block.begin, block.end))

        # load mask
        mask = ds_mask[bb]
        # if we don't have any data in the mask,
        # write 0 for max-id
        if np.sum(mask) == 0:
            with open(res_file, 'w') as f:
                json.dump({'n_components': 0, 't': time.time() - t0}, f)
            continue

        # load the affinities from the slices specified in the config
        affs = []
        for aff_slice, inv_channel in zip(aff_slices, invert_channels):
            aff = ds_affs[aff_slice + bb]
            if aff.dtype == np.dtype('uint8'):
                aff = aff.astype('float32') / 255.
            if inv_channel:
                aff = 1. - aff
            if aff.ndim == 3:
                aff = aff[None]
            affs.append(aff)
        affs = np.concatenate(affs, axis=0)

        # make max projection, threshold and extract connected components
        affs = np.max(affs, axis=0)
        affs = affs > boundary_threshold
        # take care of mask
        inv_mask = np.logical_not(mask)
        affs[inv_mask] = 0
        affs = vigra.analysis.labelVolumeWithBackground(affs.view('uint8'))

        # write the result to the out volume,
        # write the max-id
        ds_out[bb] = affs.astype('uint64')
        n_comp = int(affs.max()) + 1
        with open(res_file, 'w') as f:
            json.dump({'n_components': n_comp, 't': time.time() - t0}, f)


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('path', type=str)
    parser.add_argument('aff_key', type=str)
    parser.add_argument('mask_key', type=str)
    parser.add_argument('out_key', type=str)
    parser.add_argument('job_id', type=int)
    parser.add_argument('config_path', type=str)
    parser.add_argument('tmp_folder', type=str)
    args = parser.parse_args()

    threshold_blocks(args.path, args.aff_key, args.mask_key, args.out_key,
                     args.job_id, args.config_path, args.tmp_folder)
