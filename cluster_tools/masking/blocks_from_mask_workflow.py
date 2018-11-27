import os
import json
import luigi

from ..cluster_tasks import WorkflowBase
from . import blocks_from_mask as mask_tasks


class BlocksFromMaskWorkflow(WorkflowBase):
    mask_path = luigi.Parameter()
    mask_key = luigi.Parameter()

    output_path = luigi.Parameter()
    output_key_prefix = luigi.Parameter()

    shape = luigi.ListParameter()
    effective_scales = luigi.ListParameter()

    def requires(self):
        task = getattr(mask_tasks,
                       self._get_task_name('BlocksFromMask'))
        dep = self.dependency
        for scale, scale_factor in enumerate(self.effecive_scales):
            output_key = self.output_key_prefix + 's%i' % scale
            dep = task(tmp_folder=self.tmp_folder, max_jobs=self.max_jobs,
                       config_dir=self.config_dir,
                       mask_path=self.mask_path, mask_key=self.mask_key,
                       output_path=self.output_path, output_key=output_key)
        return dep

    @staticmethod
    def get_config():
        configs = super(BlocksFromMaskWorkflow, BlocksFromMaskWorkflow).get_config()
        configs.update({'blocks_from_mask':
                        mask_tasks.BlocksFromMaskLocal.default_task_config()})
        return configs
