# coding:utf-8
# Copyright (c) 2019  PaddlePaddle Authors. All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License"
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

from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

import argparse
import os
import platform
import socket
import json
import paddlehub as hub
from paddlehub.commands.base_command import BaseCommand, ENTRY
from paddlehub.serving import app_single as app
from paddlehub.common.dir import CONF_HOME
from paddlehub.common.hub_server import CacheUpdater
import multiprocessing
import time
import signal

if platform.system() == "Windows":

    class StandaloneApplication(object):
        def __init__(self):
            pass

        def load_config(self):
            pass

        def load(self):
            pass
else:
    import gunicorn.app.base

    class StandaloneApplication(gunicorn.app.base.BaseApplication):
        def __init__(self, app, options=None):
            self.options = options or {}
            self.application = app
            super(StandaloneApplication, self).__init__()

        def load_config(self):
            config = {
                key: value
                for key, value in self.options.items()
                if key in self.cfg.settings and value is not None
            }
            for key, value in config.items():
                self.cfg.set(key.lower(), value)

        def load(self):
            return self.application


def number_of_workers():
    return (multiprocessing.cpu_count() * 2) + 1


def pid_is_exist(pid):
    try:
        os.kill(pid, 0)
    except:
        return False
    else:
        return True


class ServingCommand(BaseCommand):
    name = "serving"
    module_list = []

    def __init__(self, name):
        super(ServingCommand, self).__init__(name)
        self.show_in_help = True
        self.description = "Start Module Serving or Bert Service for online predicting."
        self.parser = argparse.ArgumentParser(
            description=self.__class__.__doc__,
            prog='%s %s [COMMAND]' % (ENTRY, name),
            usage='%(prog)s',
            add_help=True)
        self.parser.add_argument("command")
        self.parser.add_argument("sub_command")
        self.parser.add_argument("bert_service", nargs="?")
        self.sub_parse = self.parser.add_mutually_exclusive_group(
            required=False)
        self.parser.add_argument(
            "--use_gpu", action="store_true", default=False)
        self.parser.add_argument(
            "--use_multiprocess", action="store_true", default=False)
        self.parser.add_argument("--modules", "-m", nargs="+")
        self.parser.add_argument("--config", "-c", nargs="?")
        self.parser.add_argument("--port", "-p", nargs="?", default=8866)
        self.parser.add_argument("--gpu", "-i", nargs="?", default=0)

    def dump_pid_file(self):
        pid = os.getpid()
        filepath = os.path.join(CONF_HOME,
                                "serving_" + str(self.args.port) + ".json")
        if os.path.exists(filepath):
            os.remove(filepath)
        with open(filepath, "w") as fp:
            info = {
                "pid": pid,
                "module": self.args.modules,
                "start_time": time.time()
            }
            json.dump(info, fp)

    @staticmethod
    def load_pid_file(filepath, port=None):
        if port is None:
            port = os.path.basename(filepath).split(".")[0].split("_")[1]
        if not os.path.exists(filepath):
            print(
                "PaddleHub Serving config file is not exists, "
                "please confirm the port [%s] you specified is correct." % port)
            return False
        with open(filepath, "r") as fp:
            info = json.load(fp)
            return info

    def stop_serving(self, port):
        filepath = os.path.join(CONF_HOME, "serving_" + str(port) + ".json")
        info = self.load_pid_file(filepath, port)
        if info is False:
            return
        pid = info["pid"]
        module = info["module"]
        start_time = info["start_time"]
        if os.path.exists(filepath):
            os.remove(filepath)

        if not pid_is_exist(pid):
            print("PaddleHub Serving has been stopped.")
            return
        print("PaddleHub Serving will stop.")
        CacheUpdater(
            "hub_serving_stop",
            module=module,
            addition={
                "period_time": time.time() - start_time
            }).start()
        if platform.system() == "Windows":
            os.kill(pid, signal.SIGTERM)
        else:
            os.killpg(pid, signal.SIGTERM)

    @staticmethod
    def start_bert_serving(args):
        if platform.system() != "Linux":
            print("Error. Bert Service only support linux.")
            return False

        if ServingCommand.is_port_occupied("127.0.0.1", args.port) is True:
            print("Port %s is occupied, please change it." % args.port)
            return False

        from paddle_gpu_serving.run import BertServer
        bs = BertServer(with_gpu=args.use_gpu)
        bs.with_model(model_name=args.modules[0])
        bs.run(gpu_index=args.gpu, port=int(args.port))

    @staticmethod
    def is_port_occupied(ip, port):
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            s.connect((ip, int(port)))
            s.shutdown(2)
            return True
        except:
            return False

    @staticmethod
    def preinstall_modules(modules):
        configs = []
        module_exist = {}
        if modules is not None:
            for module in modules:
                module_name = module if "==" not in module else \
                module.split("==")[0]
                module_version = None if "==" not in module else \
                module.split("==")[1]
                if module_exist.get(module_name, "") != "":
                    print(module_name, "==", module_exist.get(module_name),
                          " will be ignored cause new version is specified.")
                    configs.pop()
                module_exist.update({module_name: module_version})
                try:
                    CacheUpdater(
                        "hub_serving_start",
                        module=module_name,
                        version=module_version).start()
                    m = hub.Module(name=module_name, version=module_version)
                    method_name = m.desc.attr.map.data['default_signature'].s
                    if method_name == "":
                        raise RuntimeError("{} cannot be use for "
                                           "predicting".format(module_name))
                    configs.append({
                        "module": module_name,
                        "version": m.version,
                        "category": str(m.type).split("/")[0].upper()
                    })
                except Exception as err:
                    print(err, ", start PaddleHub Serving unsuccessfully.")
                    exit(1)
            return configs

    def start_app_with_file(self, configs, workers):
        port = configs.get("port", 8866)
        if ServingCommand.is_port_occupied("127.0.0.1", port) is True:
            print("Port %s is occupied, please change it." % port)
            return False

        modules = configs.get("modules_info")
        module = [str(i["module"]) + "==" + str(i["version"]) for i in modules]
        module_info = ServingCommand.preinstall_modules(module)
        for index in range(len(module_info)):
            modules[index].update(module_info[index])
        options = {
            "bind": "0.0.0.0:%s" % port,
            "workers": workers,
            "pid": "./pid.txt"
        }

        configs["modules_info"] = modules
        self.dump_pid_file()
        StandaloneApplication(
            app.create_app(init_flag=False, configs=configs), options).run()

    def start_single_app_with_file(self, configs):
        use_gpu = configs.get("use_gpu", False)
        port = configs.get("port", 8866)
        if ServingCommand.is_port_occupied("127.0.0.1", port) is True:
            print("Port %s is occupied, please change it." % port)
            return False
        configs = configs.get("modules_info")
        module = [str(i["module"]) + "==" + str(i["version"]) for i in configs]
        module_info = ServingCommand.preinstall_modules(module)
        for index in range(len(module_info)):
            configs[index].update(module_info[index])
        self.dump_pid_file()
        app.run(use_gpu, configs=configs, port=port)

    @staticmethod
    def start_multi_app_with_file(configs):
        port = configs.get("port", 8866)
        if ServingCommand.is_port_occupied("127.0.0.1", port) is True:
            print("Port %s is occupied, please change it." % port)
            return False
        workers = configs.get("workers", number_of_workers())
        options = {"bind": "0.0.0.0:%s" % port, "workers": workers}
        StandaloneApplication(
            app.create_app(init_flag=False, configs=configs), options).run()
        print("PaddleHub Serving has been stopped.")

    def start_app_with_args(self, workers):
        module = self.args.modules
        if module is not None:
            use_gpu = self.args.use_gpu
            port = self.args.port
            if ServingCommand.is_port_occupied("127.0.0.1", port) is True:
                print("Port %s is occupied, please change it." % port)
                return False
            module_info = ServingCommand.preinstall_modules(module)
            [
                item.update({
                    "batch_size": 1,
                    "queue_size": 20
                }) for item in module_info
            ]
            options = {"bind": "0.0.0.0:%s" % port, "workers": workers}
            configs = {"use_gpu": use_gpu, "modules_info": module_info}
            self.dump_pid_file()
            StandaloneApplication(
                app.create_app(init_flag=False, configs=configs),
                options).run()
        else:
            print("Lack of necessary parameters!")

    def start_single_app_with_args(self):
        module = self.args.modules
        if module is not None:
            use_gpu = self.args.use_gpu
            port = self.args.port
            if ServingCommand.is_port_occupied("127.0.0.1", port) is True:
                print("Port %s is occupied, please change it." % port)
                return False
            module_info = ServingCommand.preinstall_modules(module)
            [
                item.update({
                    "batch_size": 1,
                    "queue_size": 20
                }) for item in module_info
            ]
            self.dump_pid_file()
            app.run(use_gpu, configs=module_info, port=port)
        else:
            print("Lack of necessary parameters!")

    def start_multi_app_with_args(self):
        module = self.args.modules
        if module is not None:
            use_gpu = self.args.use_gpu
            port = self.args.port
            workers = number_of_workers()
            if ServingCommand.is_port_occupied("127.0.0.1", port) is True:
                print("Port %s is occupied, please change it." % port)
                return False
            module_info = ServingCommand.preinstall_modules(module)
            [
                item.update({
                    "batch_size": 1,
                    "queue_size": 20
                }) for item in module_info
            ]
            options = {"bind": "0.0.0.0:%s" % port, "workers": workers}
            configs = {"use_gpu": use_gpu, "modules_info": module_info}
            StandaloneApplication(
                app.create_app(init_flag=False, configs=configs),
                options).run()
            print("PaddleHub Serving has been stopped.")
        else:
            print("Lack of necessary parameters!")

    def start_serving(self):
        config_file = self.args.config
        if config_file is not None:
            if os.path.exists(config_file):
                with open(config_file, "r") as fp:
                    configs = json.load(fp)
                    use_multiprocess = configs.get("use_multiprocess", False)
                    if platform.system() == "Windows":
                        print(
                            "Warning: Windows cannot use multiprocess working "
                            "mode, PaddleHub Serving will switch to single process mode"
                        )
                        ServingCommand.start_single_app_with_file(configs)
                    else:
                        if use_multiprocess is True:
                            self.start_app_with_file(configs,
                                                     number_of_workers())
                        else:
                            self.start_app_with_file(configs, 1)

            else:
                print("config_file ", config_file, "not exists.")
        else:
            if platform.system() == "Windows":
                print(
                    "Warning: Windows cannot use multiprocess working "
                    "mode, PaddleHub Serving will switch to single process mode"
                )
                self.start_single_app_with_args()
            else:
                if self.args.use_multiprocess is True:
                    self.start_app_with_args(number_of_workers())
                else:
                    self.start_app_with_args(1)

    @staticmethod
    def show_help():
        str = "serving <option>\n"
        str += "\tManage PaddleHub Serving.\n"
        str += "sub command:\n"
        str += "1. start\n"
        str += "\tStart PaddleHub Serving.\n"
        str += "2. stop\n"
        str += "\tStop PaddleHub Serving.\n"
        str += "3. start bert_service\n"
        str += "\tStart Bert Service.\n"
        str += "\n"
        str += "[start] option:\n"
        str += "--modules/-m [module1==version, module2==version...]\n"
        str += "\tPre-install modules via the parameter list.\n"
        str += "--port/-p XXXX\n"
        str += "\tUse port XXXX for serving.\n"
        str += "--use_gpu\n"
        str += "\tUse gpu for predicting if you specify the parameter.\n"
        str += "--use_multiprocess\n"
        str += "\tChoose multoprocess mode, cannot be use on Windows.\n"
        str += "--config/-c file_path\n"
        str += "\tUse configs in file to start PaddleHub Serving. "
        str += "Other parameters will be ignored if you specify the parameter.\n"
        str += "\n"
        str += "[stop] option:\n"
        str += "--port/-p XXXX\n"
        str += "\tStop PaddleHub Serving on port XXXX safely.\n"
        str += "\n"
        str += "[start bert_service] option:\n"
        str += "--modules/-m\n"
        str += "\tPre-install modules via the parameter.\n"
        str += "--port/-p XXXX\n"
        str += "\tUse port XXXX for serving.\n"
        str += "--use_gpu\n"
        str += "\tUse gpu for predicting if specifies the parameter.\n"
        str += "--gpu\n"
        str += "\tSpecify the GPU devices to use.\n"
        print(str)

    def execute(self, argv):
        try:
            self.args = self.parser.parse_args()
        except:
            ServingCommand.show_help()
            return False
        if self.args.sub_command == "start":
            if self.args.bert_service == "bert_service":
                ServingCommand.start_bert_serving(self.args)
            elif self.args.bert_service is None:
                self.start_serving()
            else:
                ServingCommand.show_help()
        elif self.args.sub_command == "stop":
            if self.args.bert_service == "bert_service":
                print("Please stop Bert Service by kill process by yourself")
            elif self.args.bert_service is None:
                self.stop_serving(port=self.args.port)
        else:
            ServingCommand.show_help()


command = ServingCommand.instance()
