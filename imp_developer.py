# Copyright (c) 2016 Electric Imp
# This file is licensed under the MIT License
# http://opensource.org/licenses/MIT

import base64
import datetime
import json
import os
import re
import shutil
import subprocess
import sys
import urllib

import imp
import sublime
import sublime_plugin

# Import AdvancedNewFile resources
sys.path.append(os.path.join(os.path.dirname(__file__), "anf"))
from advanced_new_file.commands import AdvancedNewFileNew

sys.path.append(os.path.join(os.path.dirname(__file__), "."))
# Import resources
if 'plugin_resources' in sys.modules:
  imp.reload(sys.modules['plugin_resources.strings'])
from plugin_resources.strings import *

# Append requests module to the system module path
sys.path.append(os.path.join(os.path.dirname(__file__), "requests"))
import requests

# Append future (async) requests
sys.path.append(os.path.join(os.path.dirname(__file__), "requests-futures"))
from requests_futures.sessions import FuturesSession

# Generic plugin constants
PL_BUILD_API_URL         = "https://build.electricimp.com/v4/"
PL_SETTINGS_FILE         = "ImpDeveloper.sublime-settings"
PL_DEBUG_FLAG            = "debug"
PL_AGENT_URL             = "https://agent.electricimp.com/{}"
PL_WIN_PROGRAMS_DIR_32   = "C:\\Program Files (x86)\\"
PL_WIN_PROGRAMS_DIR_64   = "C:\\Program Files\\"
PL_LOG_START_TIME        = "2000-01-01T00:00:00.000+00:00"
PL_LOGS_UPDATE_PERIOD    = 5000 # ms
PL_ERROR_REGION_KEY      = "electric-imp-error-region-key"
PL_VIEW_STATUS_KEY       = "electric-imp-view-status-key"

# Electric Imp project specific constants
PR_DEFAULT_PROJECT_NAME  = "electric-imp-project"
PR_TEMPLATE_DIR_NAME     = "project-template"
PR_PROJECT_FILE_TEMPLATE = "_project_name_.sublime-project"
PR_SETTINGS_FILE         = "electric-imp.settings"
PR_BUILD_API_KEY_FILE    = "build-api.key"
PR_SOURCE_DIRECTORY      = "src"
PR_SETTINGS_DIRECTORY    = "settings"
PR_BUILD_DIRECTORY       = "build"
PR_DEVICE_FILE_NAME      = "device.nut"
PR_AGENT_FILE_NAME       = "agent.nut"
PR_PREPROCESSED_PREFIX   = "preprocessed."

PR_INITIAL_SRC_CONTENT   = "####################################\n" \
                           "## {} source code goes here\n" \
                           "####################################\n" \
                           "\n"

# Electric Imp settings and project properties
EI_BUILD_API_KEY         = "build-api-key"
EI_MODEL_ID              = "model-id"
EI_DEVICE_FILE           = "device-file"
EI_AGENT_FILE            = "agent-file"
EI_DEVICE_ID             = "device-id"

# Global variables
plugin_settings = None
project_env_map = {}

class ProjectManager:
    """Electric Imp project specific fuctionality"""

    def __init__(self, window):
        self.window = window

    @staticmethod
    def get_settings_dir(window):
        project_file_name = window.project_file_name()
        if project_file_name:
            project_dir = os.path.dirname(project_file_name)
            return os.path.join(project_dir, PR_SETTINGS_DIRECTORY)

    @staticmethod
    def dump_map_to_json_file(filename, map):
        with open(filename, "w") as f:
            json.dump(map, f)

    @staticmethod
    def get_settings_file_path(window, filename):
        settings_dir = ProjectManager.get_settings_dir(window)
        if settings_dir and filename:
            return os.path.join(settings_dir, filename)

    @staticmethod
    def is_electric_imp_project_window(window):
        settings_filename = ProjectManager.get_settings_file_path(window, PR_SETTINGS_FILE)
        return settings_filename is not None and os.path.exists(settings_filename)

    def save_settings(self, filename, settings):
        self.dump_map_to_json_file(ProjectManager.get_settings_file_path(self.window, filename), settings)

    def load_settings_file(self, filename):
        path = ProjectManager.get_settings_file_path(self.window, filename)
        if path and os.path.exists(path):
            with open(path) as f:
                return json.load(f)

    def load_settings(self):
        return self.load_settings_file(PR_SETTINGS_FILE)

    def get_build_api_key(self):
        api_key_map = self.load_settings_file(PR_BUILD_API_KEY_FILE)
        if api_key_map:
            return api_key_map.get(EI_BUILD_API_KEY)

    def get_source_directory_path(self):
        return os.path.join(os.path.dirname(self.window.project_file_name()), PR_SOURCE_DIRECTORY)

    def get_build_directory_path(self):
        return os.path.join(os.path.dirname(self.window.project_file_name()), PR_BUILD_DIRECTORY)


class Env:
    """Window (project) specific environment object"""

    def __init__(self, window):
        # Link back to the window
        self.window = window

        # Plugin text area
        self.terminal = None
        # Timestamp for the last log shown in the Plugin text area
        self.logs_timestamp = PL_LOG_START_TIME

        # UI Manager
        self.ui_manager = UIManager(window)
        # Electric Imp Project manager
        self.project_manager = ProjectManager(window)
        # Preprocessor
        self.code_processor = Preprocessor(self.project_manager)

        # Temp variables
        self.tmp_model = None
        self.tmp_device_ids = None

        # Check settings callback
        self.tmp_check_settings_callback = None

    @staticmethod
    def For(window):
        global project_env_map
        return project_env_map.get(window.project_file_name())

    @staticmethod
    def get_existing_or_create_env_for(window):
        global project_env_map

        env = Env.For(window)
        if not env:
            env = Env(window)
            project_env_map[window.project_file_name()] = env
            log_debug(
                "  [ ] Adding new project window: " + str(window) +
                ", total windows now: " + str(len(project_env_map)))
        return env


class UIManager:
    """Electric Imp plugin UI manager"""

    STATUS_UPDATE_PERIOD_SEC = 0.5

    def __init__(self, window):
        self.keep_updating_status = False
        self.window = window

    def create_new_console(self):
        env = Env.For(self.window)
        env.terminal = self.window.get_output_panel("textarea")
        env.logs_timestamp = PL_LOG_START_TIME

    def write_to_console(self, text):
        terminal = Env.For(self.window).terminal
        if terminal:
            terminal.set_read_only(False)
            terminal.run_command("append", {"characters": text + "\n"})
            terminal.set_read_only(True)

    def init_tty(self):
        env = Env.For(self.window)
        if not env.terminal:
            self.create_new_console()
        self.show_console()

    def show_console(self):
        self.window.run_command("show_panel", {"panel": "output.textarea"})

    def show_path_selector(self, caption, default_path, on_path_selected):
        # TODO: Implement path selection autocomplete (CSE-70)
        self.window.show_input_panel(caption, default_path, on_path_selected, None, None)

    def start_waiting_for_response_status_update(self):
        self.keep_updating_status = True
        self.update_wait_for_response_status()

    def stop_updating_status(self):
        self.keep_updating_status = False

    def update_wait_for_response_status(self):
        if not self.keep_updating_status:
            self.clear_status_message()

        if not hasattr(self.update_wait_for_response_status, "counter"):
            self.update_wait_for_response_status.counter = 0

        counter = self.update_wait_for_response_status.counter

        suffix = " "
        for i in range(counter):
            suffix += "."

        self.set_status_message(STR_STATUS_WAITING_FOR_RESPONSE.format(suffix))
        self.update_wait_for_response_status.counter = (counter + 1) % 4

        sublime.set_timeout_async(self.update_wait_for_response_status, UIManager.STATUS_UPDATE_PERIOD_SEC)

    def set_status_message(self, message):
        self.window.active_view().set_status(PL_VIEW_STATUS_KEY, message)

    def clear_status_message(self):
        self.window.active_view().set_status(key=PL_VIEW_STATUS_KEY, value="")


class HTTPConnection:
    """Implementation of all the Electric Imp connection functionality"""

    @staticmethod
    def __base64_encode(str):
        return base64.b64encode(str.encode()).decode()

    @staticmethod
    def __get_http_headers(key):
        return {
            "Authorization": "Basic " + HTTPConnection.__base64_encode(key),
            "Content-Type": "application/json"
        }

    @staticmethod
    def is_build_api_key_valid(key):
        return requests.get(PL_BUILD_API_URL + "models",
                            headers=HTTPConnection.__get_http_headers(key)).status_code == requests.codes.ok

    @staticmethod
    def get(key, url):
        return requests.get(url, headers=HTTPConnection.__get_http_headers(key))

    @staticmethod
    def post(key, url, data=None):
        return requests.post(url, data=data, headers=HTTPConnection.__get_http_headers(key))

    @staticmethod
    def put(key, url, data=None):
        return requests.put(url, data=data, headers=HTTPConnection.__get_http_headers(key))

    @staticmethod
    def is_response_valid(response):
        return response.status_code in [
            requests.codes.ok,
            requests.codes.created,
            requests.codes.accepted
        ]


class SourceType():
    AGENT = 0
    DEVICE = 1


class Preprocessor:
    """Preprocessor and Builder specific implementation"""

    def __init__(self, project_manager):
        self.line_table = {SourceType.AGENT: None, SourceType.DEVICE: None}

    @staticmethod
    def get_root_nodejs_dir_path():
        result = None
        platform = sublime.platform()
        if platform == "windows":
            path64 = PL_WIN_PROGRAMS_DIR_64 + "nodejs\\"
            path32 = PL_WIN_PROGRAMS_DIR_32 + "nodejs\\"
            if os.path.exists(path64):
                result = path64
            elif os.path.exists(path32):
                result = path32
        elif platform in ["linux", "osx"]:
            bin_dir = "bin/node"
            js_dir1 = "/usr/local/nodejs/"
            js_dir2 = "/usr/local/"
            if os.path.exists(os.path.join(js_dir1, bin_dir)):
                result = js_dir1
            elif os.path.exists(os.path.join(js_dir2, bin_dir)):
                result = js_dir2
        return result

    def get_node_path(self):
        platform = sublime.platform()
        if platform == "windows":
            return self.get_root_nodejs_dir_path() + "bin\\node"
        elif platform in ["linux", "osx"]:
            return self.get_root_nodejs_dir_path() + "bin/node"

    def get_node_cli_path(self):
        platform = sublime.platform()
        if platform == "windows":
            return self.get_root_nodejs_dir_path() + "lib\\node_modules\\Builder\\src\\cli.js"
        elif platform in ["linux", "osx"]:
            return self.get_root_nodejs_dir_path() + "lib/node_modules/Builder/src/cli.js"

    def preprocess(self, env):

        settings = env.project_manager.load_settings()
        bld_dir  = env.project_manager.get_build_directory_path()
        src_dir  = env.project_manager.get_source_directory_path()

        source_agent_filename  = os.path.join(src_dir, settings.get(EI_AGENT_FILE))
        result_agent_filename  = os.path.join(bld_dir, PR_PREPROCESSED_PREFIX + PR_AGENT_FILE_NAME)
        source_device_filename = os.path.join(src_dir, settings.get(EI_DEVICE_FILE))
        result_device_filename = os.path.join(bld_dir, PR_PREPROCESSED_PREFIX + PR_DEVICE_FILE_NAME)

        if not os.path.exists(bld_dir):
            os.makedirs(bld_dir)

        for code_files in [[
            source_agent_filename,
            result_agent_filename
        ], [
            source_device_filename,
            result_device_filename
        ]]:
            try:
                args = [
                    self.get_node_path(),
                    self.get_node_cli_path(),
                    "-l",
                    code_files[0]
                ]
                pipes = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                prep_out, prep_err = pipes.communicate()

                def strip_off_color_control(str):
                    return str.replace("\x1B[31m", "").replace("\x1B[39m", "")

                if pipes.returncode != 0:
                    reported_error = strip_off_color_control(prep_err.strip().decode("utf-8"))
                    env.ui_manager.write_to_console(STR_ERR_PREPROCESSING_ERROR.format(reported_error, pipes.returncode))
                    # Return on error
                    return None, None
                elif len(prep_err):
                    reported_error = strip_off_color_control(prep_err.strip().decode("utf-8"))
                    env.ui_manager.write_to_console(STR_ERR_PREPROCESSING_WARNING.format(reported_error))

                with open(code_files[1], "w") as output:
                    output.write(str(prep_out.decode("utf-8")))

                def substitute_string_in_file(filename, old_string, new_string):
                    with open(filename) as f:
                        s = f.read()
                        if old_string not in s:
                            return
                    with open(filename, 'w') as f:
                        s = s.replace(old_string, new_string)
                        f.write(s)

                # Change line number anchors format
                substitute_string_in_file(code_files[1], "#line", "//line")

            except subprocess.CalledProcessError as error:
                log_debug("Error running preprocessor. The process returned code: " + str(error.returncode))

        self.__build_line_table(env)
        return result_agent_filename, result_device_filename

    def __build_line_table(self, env):
        for source_type in self.line_table:
            self.line_table[source_type] = self.__build_line_table_for(source_type, env)

    def __build_line_table_for(self, source_type, env):
        # Setup the preprocessed file name based on the source type
        bld_dir = env.project_manager.get_build_directory_path()
        if source_type == SourceType.AGENT:
            preprocessed_file_path = os.path.join(bld_dir, PR_PREPROCESSED_PREFIX + PR_AGENT_FILE_NAME)
            orig_file = PR_AGENT_FILE_NAME
        elif source_type == SourceType.DEVICE:
            preprocessed_file_path = os.path.join(bld_dir, PR_PREPROCESSED_PREFIX + PR_DEVICE_FILE_NAME)
            orig_file = PR_DEVICE_FILE_NAME
        else:
            log_debug("Wrong source type")
            return

        # Parse the target file and build the code line table
        line_table = {}
        pattern = re.compile(r".*//line (\d+) \"(.+)\"")
        curr_line = 0
        orig_line = 0
        if os.path.exists(preprocessed_file_path):
            with open(preprocessed_file_path, 'r', encoding="utf-8") as f:
                while 1:
                    line_table[str(curr_line)] = (orig_file, orig_line)
                    line = f.readline()
                    if not line:
                        break
                    match = pattern.match(line)
                    if match:
                        orig_line = int(match.group(1)) - 1
                        orig_file = match.group(2)
                    orig_line += 1
                    curr_line += 1

        return line_table

    # Converts error location in the preprocessed code into the original filename and line number
    def get_error_location(self, source_type, line, env):
        if not self.line_table[source_type]:
            self.__build_line_table(env)
        code_table = self.line_table[source_type]
        return None if code_table is None else code_table[str(line)]


class BaseElectricImpCommand(sublime_plugin.WindowCommand):
    """The base class for all the Electric Imp Commands"""

    def init_env(self):
        if not hasattr(self, "env") and ProjectManager.is_electric_imp_project_window(self.window):
            self.env = Env.get_existing_or_create_env_for(self.window)

    def load_settings(self):
        return self.env.project_manager.load_settings()

    def check_settings(self, callback=None):
        # Setup pending callback
        if callback:
            self.env.tmp_check_settings_callback = callback
        else:
            callback = self.env.tmp_check_settings_callback

        # Perform the checks and prompts for appropriate settings
        if self.is_missing_build_api_key():
            self.prompt_for_build_api_key()
        elif self.is_missing_model():
            self.create_new_model()
        # elif self.is_missing_device():
        #     self.select_or_register_device()
        else:
            # All the checks passed, invoke the callback now
            if callback:
                callback()
            self.env.tmp_check_settings_callback = None

    def is_missing_build_api_key(self):
        return not self.env.project_manager.get_build_api_key()

    def prompt_for_build_api_key(self, need_to_confirm=True):
        if need_to_confirm and not sublime.ok_cancel_dialog(STR_PROVIDE_BUILD_API_KEY): return
        self.window.show_input_panel(STR_BUILD_API_KEY, "", self.on_build_api_key_provided, None, None)

    def is_missing_model(self):
        settings = self.load_settings()
        return EI_MODEL_ID not in settings or settings.get(EI_MODEL_ID) is None

    def create_new_model(self, need_to_confirm=True):
        if need_to_confirm and not sublime.ok_cancel_dialog(STR_MODEL_PROVIDE_NAME): return
        self.window.show_input_panel(STR_MODEL_NAME, "", self.on_model_name_provided, None, None)

    def on_model_name_provided(self, name):
        response = HTTPConnection.post(self.env.project_manager.get_build_api_key(),
                                       PL_BUILD_API_URL + "models/", '{"name" : "' + name + '" }')

        if not HTTPConnection.is_response_valid(response) \
                and sublime.ok_cancel_dialog(STR_MODEL_NAME_EXISTS):
            self.create_new_model(False)
            return
        elif not HTTPConnection.is_response_valid(response):
            sublime.message_dialog(STR_MODEL_FAILED_TO_CREATE)
            return

        # Save newly created model to the project settings
        settings = self.load_settings()
        settings[EI_MODEL_ID] = response.json().get("model").get("id")
        self.env.project_manager.save_settings(PR_SETTINGS_FILE, settings)

        # Check settings
        self.check_settings()

    def is_missing_device(self):
        settings = self.load_settings()
        return EI_DEVICE_ID not in settings or settings.get(EI_DEVICE_ID) is None

    def load_devices(self, input_device_ids=None, exclude_device_ids=None):
        device_ids = input_device_ids if input_device_ids else []
        device_names = []

        response = HTTPConnection.get(self.env.project_manager.get_build_api_key(),
                                      PL_BUILD_API_URL + "devices/")
        all_devices = response.json().get("devices")

        if exclude_device_ids is None:
            exclude_device_ids = []

        def get_device_display_name(device):
            name = d.get("name")
            return name if name else device.get("id")

        if input_device_ids:
            for d_id in input_device_ids:
                for d in all_devices:
                    if d.get("id") == d_id and d_id not in exclude_device_ids:
                        device_names.append(get_device_display_name(d))
                        break
        else:
            for d in all_devices:
                if d.get("id") not in exclude_device_ids:
                    device_ids.append(d.get("id"))
                    device_names.append(get_device_display_name(d))

        return device_ids, device_names

    def prompt_add_device_to_model(self, model, exclude_ids, need_to_confirm=True):
        (device_ids, device_names) = self.load_devices(exclude_device_ids=exclude_ids)

        if len(device_ids) == 0:
            sublime.message_dialog(STR_NO_DEVICES_AVAILABLE)
            return

        if need_to_confirm and not sublime.ok_cancel_dialog(STR_MODEL_REGISTER_DEVICE): return

        self.env.tmp_model = model
        self.env.tmp_device_ids = device_ids

        self.window.show_quick_panel(device_names, self.on_device_to_register_selected)

    def on_device_to_register_selected(self, index):
        model = self.env.tmp_model
        device_id = self.env.tmp_device_ids[index]

        response = HTTPConnection.put(self.env.project_manager.get_build_api_key(),
                                      PL_BUILD_API_URL + "devices/" + device_id,
                                      '{"model_id": "' + model.get("id") + '"}')
        if not HTTPConnection.is_response_valid(response):
            sublime.message_dialog(STR_MODEL_REGISTER_FAILED)

        # Once the device is registered, select this device
        self.on_device_selected(index)

        sublime.message_dialog(STR_MODEL_IMP_REGISTERED)

        self.env.tmp_model = None
        self.env.tmp_device_ids = None

    def load_this_model(self):
        # We assume the model is set up already
        model_id = self.load_settings().get(EI_MODEL_ID)
        response = HTTPConnection.get(self.env.project_manager.get_build_api_key(),
                                      PL_BUILD_API_URL + "models/" + str(model_id))
        if HTTPConnection.is_response_valid(response):
            return response.json()

    def select_or_register_device(self, need_to_confirm=True, force_register=False):

        model = self.load_this_model()
        device_ids = model.get("model").get("devices") if model else None

        if (force_register or not model or not device_ids or len(device_ids) == 0) and \
                (not need_to_confirm or sublime.ok_cancel_dialog(STR_MODEL_HAS_NO_DEVICES)):
            self.prompt_add_device_to_model(model.get("model"), exclude_ids=device_ids, need_to_confirm=False)
            return

        if need_to_confirm and not sublime.ok_cancel_dialog(STR_SELECT_DEVICE): return
        (Env.For(self.window).tmp_device_ids, device_names) = self.load_devices(input_device_ids=device_ids)
        self.window.show_quick_panel(device_names, self.on_device_selected)

    def on_device_selected(self, index):
        settings = self.load_settings()
        new_device_id = Env.For(self.window).tmp_device_ids[index]
        old_device_id = None if EI_DEVICE_ID not in settings else settings.get(EI_DEVICE_ID)
        if new_device_id != old_device_id:
            log_debug("New device selected: saving new settings file and restarting the console...")
            # Update the device id
            settings[EI_DEVICE_ID] = Env.For(self.window).tmp_device_ids[index]
            self.env.project_manager.save_settings(PR_SETTINGS_FILE, settings)
            # Clean up the terminal window
            self.env.ui_manager.create_new_console()
            self.env.ui_manager.show_console()
        else:
            log_debug("Newly selected device is the same as the old one. Nothing to do.")
        # Clean up temporary variables
        self.env.tmp_device_ids = None
        # Loop back to the main settings check
        self.check_settings()

    def on_build_api_key_provided(self, key):
        log_debug("build api key provided: " + key)
        if HTTPConnection.is_build_api_key_valid(key):
            log_debug("build API key is valid")
            self.env.project_manager.save_settings(PR_BUILD_API_KEY_FILE, {
                EI_BUILD_API_KEY: key
            })
        else:
            if sublime.ok_cancel_dialog(STR_INVALID_API_KEY):
                self.prompt_for_build_api_key(False)

        # Loop back to the main settings check
        self.check_settings()

    def print_to_tty(self, text):
        env = Env.For(self.window)
        if env:
            self.env.ui_manager.write_to_console(text)
        else:
            print(STR_ERR_CONSOLE_NOT_FOUND.format(text))

    def is_enabled(self):
        return ProjectManager.is_electric_imp_project_window(self.window)


class ImpBuildAndRunCommand(BaseElectricImpCommand):
    """Build and Run command implementation"""

    def run(self):
        self.init_env()
        self.env.ui_manager.init_tty()
        # Clean up all the error marks first
        for view in self.window.views():
            view.erase_regions(PL_ERROR_REGION_KEY)

        def check_settings_callback():
            if self.env.project_manager.get_build_api_key() is None:
                log_debug("The build API file is missing, please check the settings")
                return

            # Save all the views first
            self.save_all_current_window_views()

            # Preprocess the sources
            agent_filename, device_filename = self.env.code_processor.preprocess(self.env)

            if not agent_filename and not device_filename:
                # Error happened during preprocessing, nothing to do.
                return

            if not os.path.exists(agent_filename) or not os.path.exists(device_filename):
                log_debug("Can't find code files")
                sublime.message_dialog(STR_CODE_IS_ABSENT.format(self.get_settings_file_path(PR_SETTINGS_FILE)))

            agent_code = self.read_file(agent_filename)
            device_code = self.read_file(device_filename)

            settings = self.load_settings()
            url = PL_BUILD_API_URL + "models/" + settings.get(EI_MODEL_ID) + "/revisions"
            data = '{"agent_code": ' + json.dumps(agent_code) + ', "device_code" : ' + json.dumps(device_code) + ' }'
            response = HTTPConnection.post(self.env.project_manager.get_build_api_key(), url, data)

            # Update the logs first
            update_log_windows(False)

            # Process response and handle errors appropriately
            self.handle_response(response, settings)

        self.check_settings(callback=check_settings_callback)

    def handle_response(self, response, settings):
        if HTTPConnection.is_response_valid(response):
            response_json = response.json()
            self.print_to_tty(STR_STATUS_REVISION_UPLOADED.format(str(response_json["revision"]["version"])))

            # Not it's time to restart the Model
            url = PL_BUILD_API_URL + "models/" + settings.get(EI_MODEL_ID) + "/restart"
            HTTPConnection.post(self.env.project_manager.get_build_api_key(), url)
        else:
            # {
            # 	'error': {
            # 		'message_short': 'Device code compile failed',
            # 		'details': {
            # 			'agent_errors': None,
            # 			'device_errors': [
            # 				{
            # 					'row': 3,
            # 					'error': 'expression expected',
            # 					'column': 24
            # 				}
            # 			]
            # 		},
            # 		'code': 'CompileFailed',
            # 		'message_full': 'Device code compile failed\nDevice Errors:\n3:24 expression expected'
            # 	},
            # 	'success': False
            # }
            def build_error_messages(errors, source_type, env):
                report = ""
                preprocessor = self.env.code_processor
                if errors is not None:
                    for e in errors:
                        log_debug("Original compilation error: " + str(e))
                        orig_file = "main"
                        orig_line = int(e["row"])
                        try:
                            orig_file, orig_line = \
                                preprocessor.get_error_location(source_type=source_type, line=(int(e["row"]) - 1), env=env)
                        except Exception as exc:
                            log_debug("Error trying to find original error source: {}".format(exc))
                            pass  # Do nothing - use read values
                        report += STR_ERR_MESSAGE_LINE.format(orig_file, orig_line, e["column"], e["error"])
                return report

            response_json = response.json()
            error = response_json["error"]
            if error and error["code"] == "CompileFailed":
                error_message = STR_ERR_DEPLOY_FAILED_WITH_ERRORS
                error_message += build_error_messages(error["details"]["agent_errors"],  SourceType.AGENT, self.env)
                error_message += build_error_messages(error["details"]["device_errors"], SourceType.DEVICE, self.env)
                self.print_to_tty(error_message)
            else:
                log_debug("Code deploy failed because of the error: {}".format(str(response_json["error"]["code"])))

    def save_all_current_window_views(self):
        log_debug("Saving all views...")
        self.window.run_command("save_all")

    @staticmethod
    def read_file(filename):
        with open(filename, 'r', encoding="utf-8") as f:
            s = f.read()
        return s


class ImpShowConsoleCommand(BaseElectricImpCommand):

    def run(self):
        self.init_env()
        self.env.ui_manager.init_tty()
        self.check_settings()
        update_log_windows(False)


class ImpSelectDeviceCommand(BaseElectricImpCommand):

    def run(self):
        self.init_env()
        self.check_settings(callback=self.select_or_register_device)


class ImpGetAgentUrlCommand(BaseElectricImpCommand):
    def run(self):
        self.init_env()
        def check_settings_callback():
            settings = self.load_settings()
            if EI_DEVICE_ID in settings:
                device_id = settings.get(EI_DEVICE_ID)
                response = HTTPConnection.get(self.env.project_manager.get_build_api_key(),
                                              PL_BUILD_API_URL + "devices/" + device_id).json()
                agent_id = response.get("device").get("agent_id")
                agent_url = PL_AGENT_URL.format(agent_id)
                sublime.set_clipboard(agent_url)
                sublime.message_dialog(STR_AGENT_URL_COPIED.format(device_id, agent_url))

        self.check_settings(callback=check_settings_callback)


class ImpCreateProjectCommand(BaseElectricImpCommand):
    def run(self):
        AdvancedNewProject(self.window, self.on_project_path_entered).run()

    @staticmethod
    def get_default_project_path():
        global plugin_settings
        default_project_path_setting = plugin_settings.get(PR_DEFAULT_PROJECT_NAME)
        if not default_project_path_setting:
            if sublime.platform() == "windows":
                default_project_path = os.path.expanduser("~\\" + PR_DEFAULT_PROJECT_NAME).replace("\\", "/")
            else:
                default_project_path = os.path.expanduser("~/" + PR_DEFAULT_PROJECT_NAME)
        else:
            default_project_path = default_project_path_setting
        return default_project_path

    def on_project_path_entered(self, path):
        log_debug("Project path specified: " + path)
        # self.__tmp_project_path = path
        if os.path.exists(path):
            if not sublime.ok_cancel_dialog(STR_FOLDER_EXISTS.format(path)):
                return
        self.create_project(path)
        # self.prompt_for_build_api_key()

    def create_project(self, path):
        source_dir = os.path.join(path, PR_SOURCE_DIRECTORY)
        settings_dir = os.path.join(path, PR_SETTINGS_DIRECTORY)

        log_debug("Creating project at: " + path)
        if not os.path.exists(source_dir):
            os.makedirs(source_dir)
        if not os.path.exists(settings_dir):
            os.makedirs(settings_dir)

        self.copy_project_template_file(path)
        self.copy_gitignore(path)

        # Create Electric Imp project settings file
        ProjectManager.dump_map_to_json_file(os.path.join(settings_dir, PR_SETTINGS_FILE), {
            EI_AGENT_FILE:  PR_AGENT_FILE_NAME,
            EI_DEVICE_FILE: PR_DEVICE_FILE_NAME
        })

        # Pull the latest code revision from the server
        (agent_file, device_file) = self.create_source_files_if_absent(path)

        ok = False
        try:
            # Try opening the project in the new window
            self.run_sublime_from_command_line(["-n", self.get_project_file_name(path)])
            ok = True
        except:
            log_debug("Error executing sublime: {} ".format(sys.exc_info()[0]))
            # If failed, open the project in the file browser
            self.window.run_command("open_dir", {"dir": path})

        if ok:
            def open_sources():
                # TODO: Redo: this code assumes that the last open window was appended to the window list
                last_window = sublime.windows()[-1]
                if ProjectManager.is_electric_imp_project_window(last_window):
                    last_window.open_file(agent_file)
                    last_window.open_file(device_file)

            # TODO: Redo: dirty hack: wait for awhile to open the files as the window might not be created yet
            sublime.set_timeout_async(open_sources, 10)

    @staticmethod
    def get_sublime_path():
        platform = sublime.platform()
        if platform == "osx":
            return "/Applications/Sublime Text.app/Contents/SharedSupport/bin/subl"
        elif platform == "windows":
            path64 = PL_WIN_PROGRAMS_DIR_64 + "Sublime Text 3\\sublime_text.exe"
            path32 = PL_WIN_PROGRAMS_DIR_32 + "Sublime Text 3\\sublime_text.exe"
            if os.path.exists(path64):
                return path64
            elif os.path.exists(path32):
                return path32
        elif platform == "linux":
            return "/opt/sublime/sublime_text"
        else:
            log_debug("Unknown platform: {}".format(platform))

    def run_sublime_from_command_line(self, args):
        args.insert(0, self.get_sublime_path())
        return subprocess.call(args)

    def get_project_file_name(self, path):
        return os.path.join(path, PR_PROJECT_FILE_TEMPLATE.replace("_project_name_", os.path.basename(path)))

    def copy_project_template_file(self, path):
        src = os.path.join(self.get_template_dir(), PR_PROJECT_FILE_TEMPLATE)
        dst = self.get_project_file_name(path)
        shutil.copy(src, dst)

    def copy_gitignore(self, path):
        src = os.path.join(self.get_template_dir(), ".gitignore")
        shutil.copy(src, path)

    def create_source_files_if_absent(self, path):
        source_dir  = os.path.join(path, PR_SOURCE_DIRECTORY)
        agent_file  = os.path.join(source_dir, PR_AGENT_FILE_NAME)
        device_file = os.path.join(source_dir, PR_DEVICE_FILE_NAME)

        # Create empty files if they don't exist
        if not os.path.exists(agent_file):
            with open(agent_file, 'a') as f:
                f.write(PR_INITIAL_SRC_CONTENT.format("Agent"))

        if not os.path.exists(device_file):
            with open(device_file, 'a') as f:
                f.write(PR_INITIAL_SRC_CONTENT.format("Device"))

        return agent_file, device_file

    def get_template_dir(self):
        return os.path.join(os.path.dirname(os.path.realpath(__file__)), PR_TEMPLATE_DIR_NAME)

    # Create Project menu item is always enabled regardless of the project type
    def is_enabled(self):
        return True


class ImpAddDeviceToModel(BaseElectricImpCommand):

    def run(self):
        self.init_env()
        def check_settings_callback():
            self.select_or_register_device(need_to_confirm=False, force_register=True)
        self.check_settings(callback=check_settings_callback)


class ImpRemoveDeviceFromModel(BaseElectricImpCommand):

    def run(self):
        self.init_env()
        self.check_settings(callback=self.prompt_model_to_remove_device)

    def prompt_model_to_remove_device(self, need_to_confirm=True):
        if need_to_confirm and not sublime.ok_cancel_dialog(STR_MODEL_REMOVE_DEVICE): return

        model = self.load_this_model()
        device_ids = model.get("model").get("devices") if model else None

        if not device_ids or len(device_ids) == 0:
            sublime.message_dialog(STR_MODEL_NO_DEVICES_TO_REMOVE)
            return

        (Env.For(self.window).tmp_device_ids, device_names) = self.load_devices(input_device_ids=device_ids)
        self.window.show_quick_panel(device_names, self.on_remove_device_selected)

    def on_remove_device_selected(self, index):
        device_id = self.env.tmp_device_ids[index]
        active_device_id = self.load_settings().get(EI_DEVICE_ID)

        if device_id == active_device_id:
            sublime.message_dialog(STR_MODEL_CANT_REMOVE_ACTIVE_DEVICE)
            return

        response = HTTPConnection.put(self.env.project_manager.get_build_api_key(),
                                      PL_BUILD_API_URL + "devices/" + device_id,
                                      '{"model_id": ""}')
        if not HTTPConnection.is_response_valid(response):
            sublime.message_dialog(STR_MODEL_REMOVE_DEVICE_FAILED)
            return

        sublime.message_dialog(STR_MODEL_DEVICE_REMOVED)

        self.env.tmp_model = None
        self.env.tmp_device_ids = None


class AdvancedNewProject(AdvancedNewFileNew):

    def __init__(self, window, on_path_provided=None):
        super(AdvancedNewProject, self).__init__(window)
        self.on_path_provided = on_path_provided
        self.window = window

    def input_panel_caption(self):
        return STR_NEW_PROJECT_LOCATION

    def entered_file_action(self, path):
        if self.on_path_provided:
            self.on_path_provided(path)

    def update_status_message(self, creation_path):
            self.window.active_view().set_status(PL_VIEW_STATUS_KEY, STR_STATUS_CREATING_PROJECT.format(creation_path))

    def clear(self):
        self.window.active_view().set_status(key=PL_VIEW_STATUS_KEY, value="")


class ImpEventListener(sublime_plugin.EventListener):

    def on_post_text_command(self, view, command_name, args):
        window = view.window()
        env = Env.For(window)
        if not env or view != env.terminal:
            # Not a console window - nothing to do
            return
        selected_line = view.substr(view.line(view.sel()[0]))

        cp_error_pattern = re.compile(r"\s*File: (.+), Line: (\d+), Column: (\d+), Message: (.*)")
        rt_error_pattern = re.compile(r".*\sERROR:\s*at\s*(.*):(\d+)\s*")

        orig_file = None
        orig_line = None

        cp_match = cp_error_pattern.match(selected_line)
        if cp_match: # Compilation error message
            orig_file = cp_match.group(1)
            orig_line = int(cp_match.group(2)) - 1
        else:
            rt_match = rt_error_pattern.match(selected_line)
            if rt_match:  # Runtime error message
                orig_file = rt_match.group(1)
                orig_line = int(rt_match.group(2)) - 1

        log_debug("Selected line: " + selected_line + ", original file name: " +
                  str(orig_file) + " orig_line: " + str(orig_line))

        if orig_file is not None and orig_line is not None:

            source_dir = os.path.join(os.path.dirname(window.project_file_name()), PR_SOURCE_DIRECTORY)
            file_name  = os.path.join(source_dir, orig_file)

            if not os.path.exists(file_name):
                return

            file_view = window.open_file(file_name)

            def select_region():
                if file_view.is_loading():
                    sublime.set_timeout_async(select_region, 0)
                    return
                # First, erase all previous error marks
                file_view.erase_regions(PL_ERROR_REGION_KEY)
                # Create a new error mark
                pt = file_view.text_point(orig_line, 0)
                error_region = sublime.Region(pt)
                file_view.add_regions(PL_ERROR_REGION_KEY,
                                      [error_region],
                                      scope="keyword",
                                      icon="circle",
                                      flags=sublime.DRAW_SOLID_UNDERLINE)
                file_view.show(error_region)
            select_region()


def log_debug(text):
    global plugin_settings
    if plugin_settings.get(PL_DEBUG_FLAG):
        print("  [EI::Debug] " + text)


def plugin_loaded():
    global plugin_settings
    plugin_settings = sublime.load_settings(PL_SETTINGS_FILE)


def update_log_windows(restart_timer=True):
    global project_env_map
    try:
        for (project_path, env) in list(project_env_map.items()):
            if not ProjectManager.is_electric_imp_project_window(env.window):
                # It's not a windows that corresponds to an EI project, remove it from the list
                del project_env_map[project_path]
                log_debug("Removing project window: " + str(env.window) + ", total #: " + str(len(project_env_map)))
                continue
            device_id = env.project_manager.load_settings().get(EI_DEVICE_ID)
            timestamp = env.logs_timestamp
            if None in [device_id, timestamp, env.project_manager.get_build_api_key()]:
                # Device is not selected yet and the console is not setup for the project, nothing we can do here
                continue
            url = PL_BUILD_API_URL + "devices/" + device_id + "/logs?since=" + urllib.parse.quote(timestamp)
            response = HTTPConnection.get(env.project_manager.get_build_api_key(), url)

            # There was an error while retrieving logs from the server
            if not HTTPConnection.is_response_valid(response):
                log_debug(STR_FAILED_TO_GET_LOGS)
                continue

            response_json = response.json()
            log_size = 0 if "logs" not in response_json else len(response_json["logs"])
            if log_size > 0:
                timestamp = response_json["logs"][log_size - 1]["timestamp"]
                env.logs_timestamp = timestamp
                for log in response_json["logs"]:
                    message = log["message"]
                    if log["type"] in ["server.error", "agent.error"]:
                        # agent/device compilation errors
                        preprocessor = env.code_processor
                        pattern = re.compile(r"ERROR:\s*at\s*(.*):(\d+)")
                        match = pattern.match(log["message"])
                        # log_debug("  [ ] Original runtime error: " + log["message"] + " was " + ("recognized" if match else "unrecognized"))
                        if match:
                            file_read = match.group(1)
                            line_read = int(match.group(2)) - 1
                            try:
                                (orig_file, orig_line) = preprocessor.get_error_location(
                                    SourceType.AGENT if log["type"] == "agent.error" else SourceType.DEVICE, line_read, env)
                                message = STR_ERR_RUNTIME_ERROR.format(orig_file, orig_line)
                            except:
                                pass  # Use original message if failed to translate the error location
                    try:
                        type = {
                            "status"       : "[Server]",
                            "server.log"   : "[Device]",
                            "server.error" : "[Device]",
                            "lastexitcode" : "[Device]",
                            "agent.log"    : "[Agent] ",
                            "agent.error"  : "[Agent] "
                        }[log["type"]]
                    except KeyError:
                        log_debug("Unrecognized log type: " + log["type"])
                        type = "[Unrecognized]"
                    dt = datetime.datetime.strptime("".join(log["timestamp"].rsplit(":", 1)), "%Y-%m-%dT%H:%M:%S.%f%z")
                    env.ui_manager.write_to_console(dt.strftime('%Y-%m-%d %H:%M:%S%z') + " " + type + " " + message)
    finally:
        if restart_timer:
            sublime.set_timeout_async(update_log_windows, PL_LOGS_UPDATE_PERIOD)


update_log_windows()
