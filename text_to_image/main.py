
"""
mlperf inference benchmarking tool
"""

from __future__ import division
from __future__ import print_function
from __future__ import unicode_literals

import argparse
import array
import collections
import json
import logging
import os
import sys
import threading
import time
from queue import Queue

import mlperf_loadgen as lg
import numpy as np
import torch

import subprocess
from py_demo_server_lon import main as server_main

import dataset
import coco

from concurrent.futures import ThreadPoolExecutor, as_completed

# from sut_over_network_demo import main as 

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("main")

NANO_SEC = 1e9
MILLI_SEC = 1000

SUPPORTED_DATASETS = {
    "coco-1024": (
        coco.Coco,
        dataset.preprocess,
        coco.PostProcessCoco(),
        {"image_size": [3, 1024, 1024]},
    )
}


SCENARIO_MAP = {
    "SingleStream": lg.TestScenario.SingleStream,
    "MultiStream": lg.TestScenario.MultiStream,
    "Server": lg.TestScenario.Server,
    "Offline": lg.TestScenario.Offline,
}

SUPPORTED_PROFILES = {
    "defaults": {
        "dataset": "coco-1024",
        "backend": "pytorch",
        "model-name": "stable-diffusion-xl",
    },
    "debug": {
        "dataset": "coco-1024",
        "backend": "debug",
        "model-name": "stable-diffusion-xl",
    },
    "stable-diffusion-xl-pytorch": {
        "dataset": "coco-1024",
        "backend": "pytorch",
        "model-name": "stable-diffusion-xl",
    },
    "stable-diffusion-xl-pytorch-dist": {
        "dataset": "coco-1024",
        "backend": "pytorch-dist",
        "model-name": "stable-diffusion-xl",
    },
    "stable-diffusion-xl-migraphx": {
        "dataset": "coco-1024",
        "backend": "migraphx",
        "model-name": "stable-diffusion-xl",
    },
}

def get_args():
    parser = argparse.ArgumentParser()
    parser.add_argument('--sut-server', default=['http://t004-005:8008', 'http://t004-006:8008'], nargs='+', help='A list of server address & port')
    parser.add_argument("--dataset", choices=SUPPORTED_DATASETS.keys(), help="dataset")
    parser.add_argument("--dataset-path", required=True, help="path to the dataset")
    parser.add_argument(
        "--profile", choices=SUPPORTED_PROFILES.keys(), help="standard profiles"
    )
    parser.add_argument(
        "--scenario",
        default="SingleStream",
        help="mlperf benchmark scenario, one of " + str(list(SCENARIO_MAP.keys())),
    )
    parser.add_argument(
        "--max-batchsize",
        type=int,
        default=1,
        help="max batch size in a single inference",
    )
    parser.add_argument("--threads", default=1, type=int, help="threads")
    parser.add_argument("--accuracy", action="store_true", help="enable accuracy pass")
    parser.add_argument(
        "--find-peak-performance",
        action="store_true",
        help="enable finding peak performance pass",
    )
    parser.add_argument("--backend", help="Name of the backend", default="migraphx")
    parser.add_argument("--model-name", help="Name of the model")
    parser.add_argument("--output", default="output", help="test results")
    parser.add_argument("--qps", type=int, help="target qps")
    parser.add_argument("--model-path", help="Path to model weights")

    parser.add_argument(
        "--dtype",
        default="fp32",
        choices=["fp32", "fp16", "bf16"],
        help="dtype of the model",
    )
    parser.add_argument(
        "--device",
        default="cuda",
        choices=["cuda", "cpu", "rocm"],
        help="device to run the benchmark",
    )
    parser.add_argument(
        "--latent-framework",
        default="torch",
        choices=["torch", "numpy"],
        help="framework to load the latents",
    )
    
    parser.add_argument (
        "--multi-node", 
        default=False, 
        help="Set to True to use multi-node runs. Look into py_demo_server_lon for more information. "
    )

    # file to use mlperf rules compliant parameters
    parser.add_argument(
        "--mlperf_conf", default="mlperf.conf", help="mlperf rules config"
    )
    # file for user LoadGen settings such as target QPS
    parser.add_argument(
        "--user_conf",
        default="user.conf",
        help="user config for user LoadGen settings such as target QPS",
    )
    # file for LoadGen audit settings
    parser.add_argument(
        "--audit_conf", default="audit.config", help="config for LoadGen audit settings"
    )
    # arguments to save images
    # pass this argument for official submission
    # parser.add_argument("--output-images", action="store_true", help="Store a subset of the generated images")
    # do not modify this argument for official submission
    parser.add_argument("--ids-path", help="Path to caption ids", default="tools/sample_ids.txt")

    # below will override mlperf rules compliant settings - don't use for official submission
    parser.add_argument("--time", type=int, help="time to scan in seconds")
    parser.add_argument("--count", type=int, help="dataset items to use")
    parser.add_argument("--debug", action="store_true", help="debug")
    parser.add_argument(
        "--performance-sample-count", type=int, help="performance sample count", default=5000
    )
    parser.add_argument(
        "--max-latency", type=float, help="mlperf max latency in pct tile"
    )
    parser.add_argument(
        "--samples-per-query",
        default=8,
        type=int,
        help="mlperf multi-stream samples per query",
    )
    args = parser.parse_args()

    # don't use defaults in argparser. Instead we default to a dict, override that with a profile
    # and take this as default unless command line give
    defaults = SUPPORTED_PROFILES["defaults"]

    if args.profile:
        profile = SUPPORTED_PROFILES[args.profile]
        defaults.update(profile)
    for k, v in defaults.items():
        kc = k.replace("-", "_")
        if getattr(args, kc) is None:
            setattr(args, kc, v)

    if args.scenario not in SCENARIO_MAP:
        parser.error("valid scanarios:" + str(list(SCENARIO_MAP.keys())))
    return args


def get_backend(backend, **kwargs):
    if backend == "pytorch":
        from backend_pytorch import BackendPytorch

        backend = BackendPytorch(**kwargs)
    
    # ? Yalu Ouyang Modification: Nov 5 2024
    elif backend == "migraphx":
        from backend_migraphx import BackendMIGraphX
        
        backend = BackendMIGraphX(**kwargs)

    elif backend == "debug":
        from backend_debug import BackendDebug

        backend = BackendDebug()
    else:
        raise ValueError("unknown backend: " + backend)
    return backend


class Item:
    """An item that we queue for processing by the thread pool."""

    def __init__(self, query_id, content_id, inputs, img=None):
        self.query_id = query_id
        self.content_id = content_id
        self.img = img
        self.inputs = inputs
        self.start = time.time()


class RunnerBase:
    def __init__(self, model, ds, threads, post_proc=None, max_batchsize=128):
        self.take_accuracy = False
        self.ds = ds
        self.model = model
        self.post_process = post_proc
        self.threads = threads
        self.take_accuracy = False
        self.max_batchsize = max_batchsize
        self.result_timing = []

    def handle_tasks(self, tasks_queue):
        pass

    def start_run(self, result_dict, take_accuracy):
        self.result_dict = result_dict
        self.result_timing = []
        self.take_accuracy = take_accuracy
        self.post_process.start()

    def run_one_item(self, qitem: Item):
        # run the prediction
        processed_results = []
        try:
            results = self.model.predict(qitem.inputs)
            # log.info("[Line 254] runs fine after results")
            processed_results = self.post_process(
                results, qitem.content_id, qitem.inputs, self.result_dict
            )
            # log.info("[Line 258] runs fine after processed_results")
            if self.take_accuracy:
                self.post_process.add_results(processed_results)
            self.result_timing.append(time.time() - qitem.start)
        except Exception as ex:  # pylint: disable=broad-except
            src = [self.ds.get_item_loc(i) for i in qitem.content_id]
            log.error("[Line 262] thread: failed on contentid=%s, %s", src, ex)
            # since post_process will not run, fake empty responses
            processed_results = [[]] * len(qitem.query_id)
        finally:
            response_array_refs = []
            response = []
            for idx, query_id in enumerate(qitem.query_id):
                response_array = array.array(
                    "B", np.array(processed_results[idx], np.uint8).tobytes()
                    # "B", np.array(processed_results[idx], np.uint64).tobytes()
                )
                response_array_refs.append(response_array)
                bi = response_array.buffer_info()
                response.append(lg.QuerySampleResponse(query_id, bi[0], bi[1]))
            lg.QuerySamplesComplete(response)

    def enqueue(self, query_samples):
        idx = [q.index for q in query_samples]
        query_id = [q.id for q in query_samples]
        if len(query_samples) < self.max_batchsize:
            data, label = self.ds.get_samples(idx)
            self.run_one_item(Item(query_id, idx, data, label))
        else:
            bs = self.max_batchsize
            for i in range(0, len(idx), bs):
                data, label = self.ds.get_samples(idx[i : i + bs])
                self.run_one_item(
                    Item(query_id[i : i + bs], idx[i : i + bs], data, label)
                )

    def finish(self):
        pass


class QueueRunner(RunnerBase):
    def __init__(self, model, ds, threads, post_proc=None, max_batchsize=128):
        super().__init__(model, ds, threads, post_proc, max_batchsize)
        self.tasks = Queue(maxsize=threads * 4)
        self.workers = []
        self.result_dict = {}

        for _ in range(self.threads):
            worker = threading.Thread(target=self.handle_tasks, args=(self.tasks,))
            worker.daemon = True
            self.workers.append(worker)
            worker.start()

    def handle_tasks(self, tasks_queue):
        """Worker thread."""
        while True:
            # log.info ('getting tasks')
            qitem = tasks_queue.get()
            # log.info ('getten tasks')
            if qitem is None:
                # None in the queue indicates the parent want us to exit
                tasks_queue.task_done()
                break
            self.run_one_item(qitem)
            # log.info ('going to task_done')
            tasks_queue.task_done()
            # log.info ('tasks done')

    def enqueue(self, query_samples):
        idx = [q.index for q in query_samples]
        query_id = [q.id for q in query_samples]
        if len(query_samples) < self.max_batchsize:
            data, label = self.ds.get_samples(idx)
            self.tasks.put(Item(query_id, idx, data, label))
        else:
            bs = self.max_batchsize
            for i in range(0, len(idx), bs):
                ie = i + bs
                data, label = self.ds.get_samples(idx[i:ie])
                self.tasks.put(Item(query_id[i:ie], idx[i:ie], data, label))

    def finish(self):
        # exit all threads
        for _ in self.workers:
            self.tasks.put(None)
        for worker in self.workers:
            worker.join()


def main(): 
    
    args = get_args()
    log.info(args)
    
    # Define the command and arguments
    # command = ['python', 'script_to_run.py', '--num', '10', '--text', 'Hello, world!']
    
    server_main (args)
    
    # command = ['python', 
    #            'py_demo_server_lon.py', 
    #            '--sut-server http://t007-001:8888 http://t006-001:8888',
    #            '--dataset=coco-1024', 
    #            '--dataset-path=/work1/zixian/ziw081/inference/text_to_image/coco2014',
    #            '--profile=stable-diffusion-xl-pytorch',
    #            '--dtype=fp16',
    #            '--device=cuda',
    #            '--time=30',
    #            '--scenario=Offline',
    #            '--max-batchsize=4'
    #         ]

    # find backend
    
    # backend = get_backend(
    #     args.backend,
    #     precision=args.dtype,
    #     device=args.device,
    #     model_path=args.model_path,
    #     batch_size=args.max_batchsize
    # )
    # Zixian: Oct 21: create a list of backends for multi-gpu
    # backends = [get_backend(
    #                 args.backend,
    #                 precision=args.dtype,
    #                 device=f'cuda:{i}',
    #                 model_path=args.model_path,
    #                 batch_size=args.max_batchsize
    #             ) 
    #             for i in [0, 1, 2, 3]]
    
    backends = [get_backend(
                    args.backend,
                    precision=args.dtype,
                    device=f'cuda:{int (i/int (args.gpu_num / 4))}',
                    model_path=args.model_path,
                    batch_size=args.max_batchsize
                ) 
                for i in np.arange (args.gpu_num)]

    
    log.info(f"Zixian: Returned from get_backends")
    
    
    if args.dtype == "fp16":
        dtype = torch.float16
    elif args.dtype == "bf16":
        dtype = torch.bfloat16
    else:
        dtype = torch.float32

    # --count applies to accuracy mode only and can be used to limit the number of images
    # for testing.
    count_override = False
    count = args.count
    if count:
        count_override = True

    # load model to backend
    # model = backend.load()
    log.info(f"Zixian: entering backend.load")
    # Zixian: Oct 21: create a list of models corresponding to each backend 
    models = [backend.load() for backend in backends]
    log.info(f"Zixian: loaded models from all backend")

    # dataset to use
    dataset_class, pre_proc, post_proc, kwargs = SUPPORTED_DATASETS[args.dataset]
    if args.backend == 'migraphx': 
        ds = dataset_class(
            data_path=args.dataset_path,
            name=args.dataset,
            pre_process=pre_proc,
            count=count,
            threads=args.threads,
            # pipe_tokenizer=model.pipe.tokenizer,
            # pipe_tokenizer_2=model.pipe.tokenizer_2,
            pipe_tokenizer=models[0].pipe.tokenizer,
            pipe_tokenizer_2=models[0].pipe.tokenizer_2,
            latent_dtype=dtype,
            latent_device=args.device,
            latent_framework=args.latent_framework,
            pipe_type=args.backend,
            **kwargs,
        )
    else: 
        ds = dataset_class(
            data_path=args.dataset_path,
            name=args.dataset,
            pre_process=pre_proc,
            count=count,
            threads=args.threads,
            pipe_tokenizer=models[0].pipe.tokenizer,
            pipe_tokenizer_2=models[0].pipe.tokenizer_2,
            pipe_tokenizer_2=models[0].pipe.tokenizer_2,
            latent_dtype=dtype,
            latent_device=args.device,
            latent_framework=args.latent_framework,
            pipe_type=args.backend,
            **kwargs,
        )
    final_results = {
        # "runtime": model.name(),
        # "version": model.version(),
        "runtime": models[0].name(),
        "version": models[0].version(),
        "time": int(time.time()),
        "args": vars(args),
        "cmdline": str(args),
    }

    mlperf_conf = os.path.abspath(args.mlperf_conf)
    if not os.path.exists(mlperf_conf):
        log.error("{} not found".format(mlperf_conf))
        sys.exit(1)

    user_conf = os.path.abspath(args.user_conf)
    if not os.path.exists(user_conf):
        log.error("{} not found".format(user_conf))
        sys.exit(1)

    audit_config = os.path.abspath(args.audit_conf)
    
    if args.accuracy:
        ids_path = os.path.abspath(args.ids_path)
        with open(ids_path) as f:
            saved_images_ids = [int(_) for _ in f.readlines()]

    if args.output:
        output_dir = os.path.abspath(args.output)
        os.makedirs(output_dir, exist_ok=True)
        os.chdir(output_dir)

    #
    # make one pass over the dataset to validate accuracy
    #
    count = ds.get_item_count()

    # warmup
    syntetic_str = "Lorem ipsum dolor sit amet, consectetur adipiscing elit"
    latents_pt = torch.rand(ds.latents.shape, dtype=dtype).to(args.device)
    # warmup_samples = [
    #     {
    #         "input_tokens": ds.preprocess(syntetic_str, model.pipe.tokenizer),
    #         "input_tokens_2": ds.preprocess(syntetic_str, model.pipe.tokenizer_2),
    #         "latents": latents_pt,
    #     }
    #     for _ in range(args.max_batchsize)
    # ]
    warmup_samples_gpus = [
        [
            {
                "input_tokens": ds.preprocess(syntetic_str, model.pipe.tokenizer),
                "input_tokens_2": ds.preprocess(syntetic_str, model.pipe.tokenizer_2),
                "caption": syntetic_str,
                "latents": latents_pt
            }
            for _ in range(int(args.max_batchsize))
        ]
        for model in models]
    
    # Zixian: Oct 21: warm up each backend 
    for idx, backend in enumerate (backends): 
        for i in range(1):
            _ = backend.predict(warmup_samples_gpus[idx])

    scenario = SCENARIO_MAP[args.scenario]
    runner_map = {
        lg.TestScenario.SingleStream: RunnerBase,
        lg.TestScenario.MultiStream: QueueRunner,
        lg.TestScenario.Server: QueueRunner,
        lg.TestScenario.Offline: QueueRunner,
    }
    
    # Zixian: Oct 21: create a list of runner
    # runner = runner_map[scenario](
    #     model, ds, args.threads, post_proc=post_proc, max_batchsize=args.max_batchsize
    # )
    runners = [runner_map[scenario](
                                model, ds, args.threads, post_proc=post_proc, max_batchsize=args.max_batchsize
                            )
                for model in models]

    # def issue_queries(query_samples):
    #     runner.enqueue(query_samples)
    def issue_queries(query_samples):
        print (f'\n\n len (query_samples): {len (query_samples)} \n\n')
        
        query_samples_len = len (query_samples)
        query_samples_seg_len = query_samples_len / len (runners)
        splitted_query_samples = []
        for idx in range (len (runners)): 
            log.info (f'\n\n\n')
            log.info (f'idx: {idx}')
            log.info (f'query_samples_len: {query_samples_len}')
            log.info (f'idx: {idx}')
            # if idx == len (runners) -1: 
            #     splitted_query_samples.append (query_samples[idx*query_samples_seg_len:])
            # else:
            #     splitted_query_samples.append (query_samples[idx*query_samples_seg_len : (idx+1)*query_samples_seg_len])
            
            splitted_query_samples.append (query_samples [int(round(query_samples_seg_len * idx)): int(round(query_samples_seg_len * (idx + 1)))])
                        
        
        with ThreadPoolExecutor(max_workers=len(runners)) as executor:
            # Map each runner to its respective sublist
            futures = {
                executor.submit(runner.enqueue, queries): runner 
                for runner, queries in zip(runners, splitted_query_samples)
            }
        
            # Optionally process the results
            for future in as_completed(futures):
                runner = futures[future]
                try:
                    result = future.result()
                    print(f'Runner {runner} enqueued successfully.')
                except Exception as exc:
                    print(f'Runner {runner} generated an exception: {exc}')

    def flush_queries():
        pass

    log_output_settings = lg.LogOutputSettings()
    log_output_settings.outdir = output_dir
    log_output_settings.copy_summary_to_stdout = False
    log_settings = lg.LogSettings()
    log_settings.enable_trace = args.debug
    log_settings.log_output = log_output_settings

    settings = lg.TestSettings()
    settings.FromConfig(mlperf_conf, args.model_name, args.scenario)
    settings.FromConfig(user_conf, args.model_name, args.scenario)
    if os.path.exists(audit_config):
        settings.FromConfig(audit_config, args.model_name, args.scenario)
    settings.scenario = scenario
    settings.mode = lg.TestMode.PerformanceOnly
    if args.accuracy:
        settings.mode = lg.TestMode.AccuracyOnly
    if args.find_peak_performance:
        settings.mode = lg.TestMode.FindPeakPerformance

    if args.time:
        # override the time we want to run
        settings.min_duration_ms = args.time * MILLI_SEC
        settings.max_duration_ms = args.time * MILLI_SEC

    if args.qps:
        qps = float(args.qps)
        settings.server_target_qps = qps
        settings.offline_expected_qps = qps

    if count_override:
        settings.min_query_count = count
        settings.max_query_count = count

    if args.samples_per_query:
        settings.multi_stream_samples_per_query = args.samples_per_query
    if args.max_latency:
        settings.server_target_latency_ns = int(args.max_latency * NANO_SEC)
        settings.multi_stream_expected_latency_ns = int(args.max_latency * NANO_SEC)

    performance_sample_count = (
        args.performance_sample_count
        if args.performance_sample_count
        else min(count, 500)
    )
    sut = lg.ConstructSUT(issue_queries, flush_queries)
    #! [Yalu Ouyang] count here affects how many items to run (even for accuracy)
    qsl = lg.ConstructQSL(
        count, performance_sample_count, ds.load_query_samples, ds.unload_query_samples
    )

    log.info("starting {}".format(scenario))
    result_dict = {"scenario": str(scenario)}
    for runner in runners: 
        runner.start_run(result_dict, args.accuracy)
    
    # with ThreadPoolExecutor(max_workers=len(runners)) as executor:
    #         # Map each runner to its respective sublist
    #         futures = {
    #             executor.submit(runner.finish(), (result_dict, args.accuracy)): runner 
    #             for runner in runners 
    #         }
        

    lg.StartTestWithLogSettings(sut, qsl, settings, log_settings, audit_config)
    
    log.info("Loadgen finished tests")

    if args.accuracy:
        post_proc.finalize(result_dict, ds, output_dir=args.output)
        final_results["accuracy_results"] = result_dict
        post_proc.save_images(saved_images_ids, ds)

    log.info("After processing accuracy")

    for runner in runners: 
        runner.finish()
        
    log.info("After runner.finish()") 
    # with ThreadPoolExecutor(max_workers=len(runners)) as executor:
    #         # Map each runner to its respective sublist
    #         futures = {
    #             executor.submit(runner.finish()): runner 
    #             for runner in runners 
    #         }
        
        
    lg.DestroyQSL(qsl)
    lg.DestroySUT(sut)

    #
    # write final results
    #
    if args.output:
        with open("results.json", "w") as f:
            json.dump(final_results, f, sort_keys=True, indent=4)


if __name__ == "__main__":
    
    args = get_args()
    if args.multi_node: 
        server_main ()
    else: 
        main()
