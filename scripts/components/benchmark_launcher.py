#
# CV is a framework for continuous verification.
#
# Copyright (c) 2018-2019 ISP RAS (http://www.ispras.ru)
# Ivannikov Institute for System Programming of the Russian Academy of Sciences
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
#

import resource

from components.exporter import Exporter
from components.launcher import *
from models.verification_result import *


TAG_BENCHMARK_CLIENT_DIR = "client dir"
TAG_TOOL_DIR = "tool dir"
TAG_OUTPUT_DIR = "output dir"
TAG_TASKS_DIR = "tasks dir"
TAG_BENCHMARK_FILE = "benchmark file"
TAG_TOOL_NAME = "tool"
TAG_POLL_INTERVAL = "poll interval"


class BenchmarkLauncher(Launcher):
    """
    Main component, which launches the given benchmark if needed and processes results.
    """
    def __init__(self, config_file, additional_config: dict, is_launch=False):
        super(BenchmarkLauncher, self).__init__(COMPONENT_BENCHMARK_LAUNCHER, config_file)
        if not os.path.exists(self.work_dir):
            os.makedirs(self.work_dir, exist_ok=True)

        # Propagate command line arguments.
        for name, val in additional_config.items():
            if val:
                self.component_config[name] = val

        # Mandatory arguments.
        self.output_dir = os.path.abspath(self.component_config[TAG_OUTPUT_DIR])
        self.tasks_dir = os.path.abspath(self.component_config[TAG_TASKS_DIR])

        # Optional arguments.
        self.tool = self.component_config.get(TAG_TOOL_NAME, DEFAULT_VERIFIER_TOOL)

        self.poll_interval = self.component_config.get(TAG_POLL_INTERVAL, BUSY_WAITING_INTERVAL)

        self.is_launch = is_launch
        self.process_dir = None
        if is_launch:
            tools_dir = os.path.abspath(self.component_config[TAG_TOOL_DIR])
            self.logger.debug("Using tool directory {}".format(tools_dir))
            os.chdir(tools_dir)
        else:
            self.logger.debug("Using working directory {}".format(self.work_dir))
            os.chdir(self.work_dir)

        self.logger.debug("Create a symbolic links for source directory {}".format(self.tasks_dir))
        update_symlink(self.tasks_dir)
        for task_dir_in in glob.glob(os.path.join(self.tasks_dir, "*")):
            if os.path.isdir(task_dir_in):
                update_symlink(task_dir_in)

    def __process_single_launch_results(self, result: VerificationResults, group_directory, queue, columns,
                                        source_file, task_name, benchmark_name):
        assert self.process_dir
        files = list()
        directories = glob.glob(os.path.join(group_directory, "{}.*files".format(benchmark_name)))
        if not directories:
            self.logger.error("Output directory '{}' format is not supported".format(group_directory))
            sys.exit(0)
        for directory in directories:
            for pattern in ["{}.{}".format(task_name, result.entrypoint), "{}".format(result.entrypoint)]:
                for name in ["{}.log".format(pattern), "{}.files".format(pattern), "{}".format(pattern)]:
                    name = os.path.join(directory, name)
                    if os.path.exists(name):
                        files.append(name)
        launch_directory = self._copy_result_files(files, self.process_dir)

        result.work_dir = launch_directory
        result.parse_output_dir(launch_directory, self.install_dir, self.result_dir_et, columns)
        self._process_coverage(result, launch_directory, [self.tasks_dir], source_file)
        if result.initial_traces > 1:
            result.filter_traces(launch_directory, self.install_dir, self.result_dir_et)
        queue.put(result)
        sys.exit(0)

    def __parse_result_file(self, file: str, group_directory: str):
        self.logger.info("Processing result file {}".format(file))
        tree = ElementTree.ElementTree()
        tree.parse(file)
        root = tree.getroot()
        results = list()
        global_spec = None
        is_spec = False
        task_name = root.attrib.get('name', '')
        benchmark_name = root.attrib.get('benchmarkname', '')
        block_id = root.attrib.get('block', "NONE")
        memory_limit = root.attrib.get('memlimit', None)
        time_limit = root.attrib.get('timelimit', None)
        cpu_cores_limit = root.attrib.get('cpuCores', None)
        options = root.attrib.get('options', '')
        config = {}
        if memory_limit:
            config[TAG_CONFIG_MEMORY_LIMIT] = memory_limit
        if time_limit:
            config[TAG_CONFIG_CPU_TIME_LIMIT] = time_limit
        if cpu_cores_limit:
            config[TAG_CONFIG_CPU_CORES_LIMIT] = cpu_cores_limit
        if options:
            config[TAG_CONFIG_OPTIONS] = options

        if block_id == "NONE":
            return None
        self.job_name_suffix = task_name
        if task_name.endswith(block_id):
            task_name = task_name[:-(len(block_id) + 1)]
            if block_id == "0":
                self.job_name_suffix = task_name
        for option in options.split():
            if is_spec:
                global_spec = str(os.path.basename(option))
                if global_spec.endswith('.spc') or global_spec.endswith('.prp'):
                    global_spec = global_spec[:-4]
                break
            if option == "-spec":
                is_spec = True

        queue = multiprocessing.Queue()
        process_pool = []
        for i in range(self.cpu_cores):
            process_pool.append(None)

        for run in root.findall('./run'):
            file_name = os.path.realpath(os.path.normpath(os.path.abspath(run.attrib['name'])))
            file_name_base = os.path.basename(file_name)
            properties = run.attrib.get('properties', global_spec)
            result = VerificationResults(None, self.config)
            result.entrypoint = file_name_base
            result.rule = properties
            result.id = "."

            columns = run.findall('./column')

            try:
                while True:
                    for i in range(self.cpu_cores):
                        if process_pool[i] and not process_pool[i].is_alive():
                            process_pool[i].join()
                            process_pool[i] = None
                        if not process_pool[i]:
                            process_pool[i] = multiprocessing.Process(target=self.__process_single_launch_results,
                                                                      name=result.entrypoint,
                                                                      args=(result, group_directory, queue, columns,
                                                                            file_name, task_name, benchmark_name))
                            process_pool[i].start()
                            raise NestedLoop
                    time.sleep(self.poll_interval)
            except NestedLoop:
                self._get_from_queue_into_list(queue, results)
            except Exception as e:
                self.logger.error("Error during processing results: {}".format(e), exc_info=True)
                kill_launches(process_pool)
        wait_for_launches(process_pool)
        self._get_from_queue_into_list(queue, results)

        self.logger.debug("Processing results")
        coverage_resources = {TAG_CPU_TIME: 0.0, TAG_WALL_TIME: 0.0, TAG_MEMORY_USAGE: 0}
        mea_resources = {TAG_CPU_TIME: 0.0, TAG_WALL_TIME: 0.0, TAG_MEMORY_USAGE: 0}
        for result in results:
            coverage_resources[TAG_MEMORY_USAGE] = max(coverage_resources[TAG_MEMORY_USAGE],
                                                       result.coverage_resources.get(TAG_MEMORY_USAGE, 0))
            mea_resources[TAG_MEMORY_USAGE] = max(mea_resources[TAG_MEMORY_USAGE],
                                                  result.mea_resources.get(TAG_MEMORY_USAGE, 0))
            coverage_resources[TAG_CPU_TIME] += result.coverage_resources.get(TAG_CPU_TIME, 0.0)
            coverage_resources[TAG_WALL_TIME] += result.coverage_resources.get(TAG_WALL_TIME, 0.0)
            mea_resources[TAG_CPU_TIME] += result.mea_resources.get(TAG_CPU_TIME, 0.0)
            mea_resources[TAG_WALL_TIME] += result.mea_resources.get(TAG_WALL_TIME, 0.0)
        report_launches, result_archive, report_components, _, report_resources = self._get_results_names()
        self._print_launches_report(report_launches, report_resources, results)
        overall_cpu_time = time.process_time() - self.start_cpu_time
        overall_wall_time = time.time() - self.start_time
        self.logger.info("Preparing report on components into file: '{}'".format(report_components))
        with open(report_components, "w") as f_report:
            f_report.write("Name;CPU;Wall;Memory\n")  # Header.
            f_report.write("{0};{1};{2};{3}\n".format(COMPONENT_BENCHMARK_LAUNCHER,
                                                      round(overall_cpu_time, ROUND_DIGITS),
                                                      round(overall_wall_time, ROUND_DIGITS),
                                                      int(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss) * 1024))
            f_report.write("{0};{1};{2};{3}\n".format(COMPONENT_MEA, round(mea_resources[TAG_CPU_TIME], ROUND_DIGITS),
                                                      round(mea_resources[TAG_WALL_TIME], ROUND_DIGITS),
                                                      mea_resources[TAG_MEMORY_USAGE]))
            f_report.write("{0};{1};{2};{3}\n".format(COMPONENT_COVERAGE,
                                                      round(coverage_resources[TAG_CPU_TIME], ROUND_DIGITS),
                                                      round(coverage_resources[TAG_WALL_TIME], ROUND_DIGITS),
                                                      coverage_resources[TAG_MEMORY_USAGE]))

        self.logger.info("Exporting results into archive: '{}'".format(result_archive))
        upload_process = multiprocessing.Process(target=self.__upload, name="upload",
                                                 args=(report_launches, report_resources, report_components,
                                                       result_archive, config))
        upload_process.start()
        upload_process.join()
        return result_archive

    def __upload(self, report_launches, report_resources, report_components, result_archive, config):
        exporter = Exporter(self.config, DEFAULT_EXPORT_DIR, self.install_dir, tool=self.tool)
        exporter.export(report_launches, report_resources, report_components, result_archive, verifier_config=config)

    def launch_benchmark(self):
        if not self.scheduler or not self.scheduler == SCHEDULER_CLOUD:
            self.logger.error("Scheduler '{}' is not supported (only cloud scheduler is currently supported)".
                              format(self.scheduler))
            exit(1)
        exec_dir = os.path.abspath(self.component_config[TAG_BENCHMARK_CLIENT_DIR])
        benchmark_name = os.path.abspath(self.component_config[TAG_BENCHMARK_FILE])
        self.logger.info("Launching benchmark {}".format(benchmark_name))

        benchmark_name_rel = os.path.basename(benchmark_name)
        if os.path.exists(benchmark_name_rel):
            os.remove(benchmark_name_rel)
        shutil.copy(benchmark_name, benchmark_name_rel)

        os.makedirs(self.output_dir, exist_ok=True)

        shutil.rmtree(self.output_dir)
        os.makedirs(self.output_dir)

        log_file_name = os.path.join(self.output_dir, CLOUD_BENCHMARK_LOG)
        with open(log_file_name, 'w') as f_log:
            # Launch group.
            command = "python3 {}/scripts/benchmark.py --no-compress-results -o {} --container {} {}". \
                format(exec_dir, self.output_dir, benchmark_name_rel, self.benchmark_args)
            self.logger.debug("Launching benchmark: {}".format(command))
            subprocess.check_call(command, shell=True, stderr=f_log, stdout=f_log)

    def process_results(self):
        xml_files = glob.glob(os.path.join(self.output_dir, '*results.*.xml'))
        uploader_config = self.config.get(UPLOADER, {})
        is_upload = uploader_config and uploader_config.get(TAG_UPLOADER_UPLOAD_RESULTS, False)
        for file in xml_files:
            self.process_dir = os.path.abspath(tempfile.mkdtemp(dir=self.work_dir))
            result_archive = self.__parse_result_file(file, self.output_dir)
            if is_upload and result_archive:
                self._upload_results(uploader_config, result_archive)
            if not self.debug:
                shutil.rmtree(self.process_dir, ignore_errors=True)
        else:
            self.logger.warning("No results xml files in output directory: {}".format(self.output_dir))
        if not self.debug:
            clear_symlink(self.tasks_dir)
            for task_dir_in in glob.glob(os.path.join(self.tasks_dir, "*")):
                clear_symlink(task_dir_in)
