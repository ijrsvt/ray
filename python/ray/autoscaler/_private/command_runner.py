from getpass import getuser
from shlex import quote
from typing import Dict
import click
import hashlib
import json
import logging
import os
import subprocess
import sys
import time
import warnings

from ray.autoscaler.command_runner import CommandRunnerInterface
from ray.autoscaler._private.constants import \
                                     DEFAULT_OBJECT_STORE_MAX_MEMORY_BYTES,\
                                     DEFAULT_OBJECT_STORE_MEMORY_PROPORTION, \
                                     NODE_START_WAIT_S
from ray.autoscaler._private.docker import check_bind_mounts_cmd, \
                                  check_docker_running_cmd, \
                                  check_docker_image, \
                                  docker_start_cmds, \
                                  with_docker_exec
from ray.autoscaler._private.log_timer import LogTimer

from ray.autoscaler._private.subprocess_output_util import (
    run_cmd_redirected, ProcessRunnerError, is_output_redirected)

from ray.autoscaler._private.cli_logger import cli_logger, cf
from ray.util.debug import log_once

logger = logging.getLogger(__name__)

# How long to wait for a node to start, in seconds
HASH_MAX_LENGTH = 10
KUBECTL_RSYNC = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "kubernetes/kubectl-rsync.sh")
MAX_HOME_RETRIES = 3
HOME_RETRY_DELAY_S = 5

_config = {"use_login_shells": True, "silent_rsync": True}


def is_rsync_silent():
    return _config["silent_rsync"]


def set_rsync_silent(val):
    """Choose whether to silence rsync output.

    Most commands will want to list rsync'd files themselves rather than
    print the default rsync spew.
    """
    _config["silent_rsync"] = val


def is_using_login_shells():
    return _config["use_login_shells"]


def set_using_login_shells(val):
    """Choose between login and non-interactive shells.

    Non-interactive shells have the benefit of receiving less output from
    subcommands (since progress bars and TTY control codes are not printed).
    Sometimes this can be significant since e.g. `pip install` prints
    hundreds of progress bar lines when downloading.

    Login shells have the benefit of working very close to how a proper bash
    session does, regarding how scripts execute and how the environment is
    setup. This is also how all commands were ran in the past. The only reason
    to use login shells over non-interactive shells is if you need some weird
    and non-robust tool to work.

    Args:
        val (bool): If true, login shells will be used to run all commands.
    """
    _config["use_login_shells"] = val


def _with_environment_variables(cmd: str,
                                environment_variables: Dict[str, object]):
    """Prepend environment variables to a shell command.

    Args:
        cmd (str): The base command.
        environment_variables (Dict[str, object]): The set of environment
            variables. If an environment variable value is a dict, it will
            automatically be converted to a one line yaml string.
    """

    as_strings = []
    for key, val in environment_variables.items():
        val = json.dumps(val, separators=(",", ":"))
        s = "export {}={};".format(key, quote(val))
        as_strings.append(s)
    all_vars = "".join(as_strings)
    return all_vars + cmd


def _with_interactive(cmd):
    force_interactive = (
        f"true && source ~/.bashrc && "
        f"export OMP_NUM_THREADS=1 PYTHONWARNINGS=ignore && ({cmd})")
    return ["bash", "--login", "-c", "-i", quote(force_interactive)]


class KubernetesCommandRunner(CommandRunnerInterface):
    def __init__(self, log_prefix, namespace, node_id, auth_config,
                 process_runner):

        self.log_prefix = log_prefix
        self.process_runner = process_runner
        self.node_id = str(node_id)
        self.namespace = namespace
        self.kubectl = ["kubectl", "-n", self.namespace]
        self._home_cached = None

    def run(
            self,
            cmd=None,
            timeout=120,
            exit_on_fail=False,
            port_forward=None,
            with_output=False,
            environment_variables: Dict[str, object] = None,
            run_env="auto",  # Unused argument.
            ssh_options_override_ssh_key="",  # Unused argument.
            shutdown_after_run=False,
    ):
        if shutdown_after_run:
            cmd += "; sudo shutdown -h now"
        if cmd and port_forward:
            raise Exception(
                "exec with Kubernetes can't forward ports and execute"
                "commands together.")

        if port_forward:
            if not isinstance(port_forward, list):
                port_forward = [port_forward]
            port_forward_cmd = self.kubectl + [
                "port-forward",
                self.node_id,
            ] + [
                "{}:{}".format(local, remote) for local, remote in port_forward
            ]
            logger.info("Port forwarding with: {}".format(
                " ".join(port_forward_cmd)))
            port_forward_process = subprocess.Popen(port_forward_cmd)
            port_forward_process.wait()
            # We should never get here, this indicates that port forwarding
            # failed, likely because we couldn't bind to a port.
            pout, perr = port_forward_process.communicate()
            exception_str = " ".join(
                port_forward_cmd) + " failed with error: " + perr
            raise Exception(exception_str)
        else:
            final_cmd = self.kubectl + ["exec", "-it"]
            final_cmd += [
                self.node_id,
                "--",
            ]
            if environment_variables:
                cmd = _with_environment_variables(cmd, environment_variables)
            cmd = _with_interactive(cmd)
            cmd_prefix = " ".join(final_cmd)
            final_cmd += cmd
            # `kubectl exec` + subprocess w/ list of args has unexpected
            # side-effects.
            final_cmd = " ".join(final_cmd)
            logger.info(self.log_prefix + "Running {}".format(final_cmd))
            try:
                if with_output:
                    return self.process_runner.check_output(
                        final_cmd, shell=True)
                else:
                    self.process_runner.check_call(final_cmd, shell=True)
            except subprocess.CalledProcessError:
                if exit_on_fail:
                    quoted_cmd = cmd_prefix + quote(" ".join(cmd))
                    logger.error(
                        self.log_prefix +
                        "Command failed: \n\n  {}\n".format(quoted_cmd))
                    sys.exit(1)
                else:
                    raise

    def run_rsync_up(self, source, target, options=None):
        options = options or {}
        if options.get("rsync_exclude"):
            if log_once("autoscaler_k8s_rsync_exclude"):
                logger.warning("'rsync_exclude' detected but is currently "
                               "unsupported for k8s.")
        if options.get("rsync_filter"):
            if log_once("autoscaler_k8s_rsync_filter"):
                logger.warning("'rsync_filter' detected but is currently "
                               "unsupported for k8s.")
        if target.startswith("~"):
            target = self._home + target[1:]

        try:
            flags = "-aqz" if is_rsync_silent() else "-avz"
            self.process_runner.check_call([
                KUBECTL_RSYNC,
                flags,
                source,
                "{}@{}:{}".format(self.node_id, self.namespace, target),
            ])
        except Exception as e:
            warnings.warn(
                self.log_prefix +
                "rsync failed: '{}'. Falling back to 'kubectl cp'".format(e),
                UserWarning)
            if target.startswith("~"):
                target = self._home + target[1:]

            self.process_runner.check_call(self.kubectl + [
                "cp", source, "{}/{}:{}".format(self.namespace, self.node_id,
                                                target)
            ])

    def run_rsync_down(self, source, target, options=None):
        if source.startswith("~"):
            source = self._home + source[1:]

        try:
            flags = "-aqz" if is_rsync_silent() else "-avz"
            self.process_runner.check_call([
                KUBECTL_RSYNC,
                flags,
                "{}@{}:{}".format(self.node_id, self.namespace, source),
                target,
            ])
        except Exception as e:
            warnings.warn(
                self.log_prefix +
                "rsync failed: '{}'. Falling back to 'kubectl cp'".format(e),
                UserWarning)
            if target.startswith("~"):
                target = self._home + target[1:]

            self.process_runner.check_call(self.kubectl + [
                "cp", "{}/{}:{}".format(self.namespace, self.node_id, source),
                target
            ])

    def remote_shell_command_str(self):
        return "{} exec -it {} -- bash".format(" ".join(self.kubectl),
                                               self.node_id)

    @property
    def _home(self):
        if self._home_cached is not None:
            return self._home_cached
        for _ in range(MAX_HOME_RETRIES - 1):
            try:
                self._home_cached = self._try_to_get_home()
                return self._home_cached
            except Exception:
                # TODO (Dmitri): Identify the exception we're trying to avoid.
                logger.info("Error reading container's home directory. "
                            f"Retrying in {HOME_RETRY_DELAY_S} seconds.")
                time.sleep(HOME_RETRY_DELAY_S)
        # Last try
        self._home_cached = self._try_to_get_home()
        return self._home_cached

    def _try_to_get_home(self):
        # TODO (Dmitri): Think about how to use the node's HOME variable
        # without making an extra kubectl exec call.
        cmd = self.kubectl + [
            "exec", "-it", self.node_id, "--", "printenv", "HOME"
        ]
        joined_cmd = " ".join(cmd)
        raw_out = self.process_runner.check_output(joined_cmd, shell=True)
        home = raw_out.decode().strip("\n\r")
        return home


class SSHOptions:
    def __init__(self, ssh_key, control_path=None, **kwargs):
        self.ssh_key = ssh_key
        self.arg_dict = {
            # Supresses initial fingerprint verification.
            "StrictHostKeyChecking": "no",
            # SSH IP and fingerprint pairs no longer added to known_hosts.
            # This is to remove a "REMOTE HOST IDENTIFICATION HAS CHANGED"
            # warning if a new node has the same IP as a previously
            # deleted node, because the fingerprints will not match in
            # that case.
            "UserKnownHostsFile": os.devnull,
            # Try fewer extraneous key pairs.
            "IdentitiesOnly": "yes",
            # Abort if port forwarding fails (instead of just printing to
            # stderr).
            "ExitOnForwardFailure": "yes",
            # Quickly kill the connection if network connection breaks (as
            # opposed to hanging/blocking).
            "ServerAliveInterval": 5,
            "ServerAliveCountMax": 3
        }
        if control_path:
            self.arg_dict.update({
                "ControlMaster": "auto",
                "ControlPath": "{}/%C".format(control_path),
                "ControlPersist": "10s",
            })
        self.arg_dict.update(kwargs)

    def to_ssh_options_list(self, *, timeout=60):
        self.arg_dict["ConnectTimeout"] = "{}s".format(timeout)
        ssh_key_option = ["-i", self.ssh_key] if self.ssh_key else []
        return ssh_key_option + [
            x for y in (["-o", "{}={}".format(k, v)]
                        for k, v in self.arg_dict.items() if v is not None)
            for x in y
        ]


class SSHCommandRunner(CommandRunnerInterface):
    def __init__(self, log_prefix, node_id, provider, auth_config,
                 cluster_name, process_runner, use_internal_ip):

        ssh_control_hash = hashlib.md5(cluster_name.encode()).hexdigest()
        ssh_user_hash = hashlib.md5(getuser().encode()).hexdigest()
        ssh_control_path = "/tmp/ray_ssh_{}/{}".format(
            ssh_user_hash[:HASH_MAX_LENGTH],
            ssh_control_hash[:HASH_MAX_LENGTH])

        self.cluster_name = cluster_name
        self.log_prefix = log_prefix
        self.process_runner = process_runner
        self.node_id = node_id
        self.use_internal_ip = use_internal_ip
        self.provider = provider
        self.ssh_private_key = auth_config.get("ssh_private_key")
        self.ssh_user = auth_config["ssh_user"]
        self.ssh_control_path = ssh_control_path
        self.ssh_ip = None
        self.ssh_proxy_command = auth_config.get("ssh_proxy_command", None)
        self.ssh_options = SSHOptions(
            self.ssh_private_key,
            self.ssh_control_path,
            ProxyCommand=self.ssh_proxy_command)

    def _get_node_ip(self):
        if self.use_internal_ip:
            return self.provider.internal_ip(self.node_id)
        else:
            return self.provider.external_ip(self.node_id)

    def _wait_for_ip(self, deadline):
        # if we have IP do not print waiting info
        ip = self._get_node_ip()
        if ip is not None:
            cli_logger.labeled_value("Fetched IP", ip)
            return ip

        interval = 10
        with cli_logger.group("Waiting for IP"):
            while time.time() < deadline and \
                    not self.provider.is_terminated(self.node_id):
                ip = self._get_node_ip()
                if ip is not None:
                    cli_logger.labeled_value("Received", ip)
                    return ip
                cli_logger.print("Not yet available, retrying in {} seconds",
                                 cf.bold(str(interval)))
                time.sleep(interval)

        return None

    def _set_ssh_ip_if_required(self):
        if self.ssh_ip is not None:
            return

        # We assume that this never changes.
        #   I think that's reasonable.
        deadline = time.time() + NODE_START_WAIT_S
        with LogTimer(self.log_prefix + "Got IP"):
            ip = self._wait_for_ip(deadline)

            cli_logger.doassert(ip is not None,
                                "Could not get node IP.")  # todo: msg
            assert ip is not None, "Unable to find IP of node"

        self.ssh_ip = ip

        # This should run before any SSH commands and therefore ensure that
        #   the ControlPath directory exists, allowing SSH to maintain
        #   persistent sessions later on.
        try:
            os.makedirs(self.ssh_control_path, mode=0o700, exist_ok=True)
        except OSError as e:
            cli_logger.warning("{}", str(e))  # todo: msg

    def _run_helper(self,
                    final_cmd,
                    with_output=False,
                    exit_on_fail=False,
                    silent=False):
        """Run a command that was already setup with SSH and `bash` settings.

        Args:
            cmd (List[str]):
                Full command to run. Should include SSH options and other
                processing that we do.
            with_output (bool):
                If `with_output` is `True`, command stdout and stderr
                will be captured and returned.
            exit_on_fail (bool):
                If `exit_on_fail` is `True`, the process will exit
                if the command fails (exits with a code other than 0).

        Raises:
            ProcessRunnerError if using new log style and disabled
                login shells.
            click.ClickException if using login shells.
        """
        try:
            # For now, if the output is needed we just skip the new logic.
            # In the future we could update the new logic to support
            # capturing output, but it is probably not needed.
            if not with_output:
                return run_cmd_redirected(
                    final_cmd,
                    process_runner=self.process_runner,
                    silent=silent,
                    use_login_shells=is_using_login_shells())
            if with_output:
                return self.process_runner.check_output(final_cmd)
            else:
                return self.process_runner.check_call(final_cmd)
        except subprocess.CalledProcessError as e:
            joined_cmd = " ".join(final_cmd)
            if not is_using_login_shells():
                raise ProcessRunnerError(
                    "Command failed",
                    "ssh_command_failed",
                    code=e.returncode,
                    command=joined_cmd)

            if exit_on_fail:
                raise click.ClickException(
                    "Command failed:\n\n  {}\n".format(joined_cmd)) from None
            else:
                fail_msg = "SSH command failed."
                if is_output_redirected():
                    fail_msg += " See above for the output from the failure."
                raise click.ClickException(fail_msg) from None

    def run(
            self,
            cmd,
            timeout=120,
            exit_on_fail=False,
            port_forward=None,
            with_output=False,
            environment_variables: Dict[str, object] = None,
            run_env="auto",  # Unused argument.
            ssh_options_override_ssh_key="",
            shutdown_after_run=False,
            silent=False):
        if shutdown_after_run:
            cmd += "; sudo shutdown -h now"
        if ssh_options_override_ssh_key:
            ssh_options = SSHOptions(ssh_options_override_ssh_key)
        else:
            ssh_options = self.ssh_options

        assert isinstance(
            ssh_options, SSHOptions
        ), "ssh_options must be of type SSHOptions, got {}".format(
            type(ssh_options))

        self._set_ssh_ip_if_required()

        if is_using_login_shells():
            ssh = ["ssh", "-tt"]
        else:
            ssh = ["ssh"]

        if port_forward:
            with cli_logger.group("Forwarding ports"):
                if not isinstance(port_forward, list):
                    port_forward = [port_forward]
                for local, remote in port_forward:
                    cli_logger.verbose(
                        "Forwarding port {} to port {} on localhost.",
                        cf.bold(local), cf.bold(remote))  # todo: msg
                    ssh += ["-L", "{}:localhost:{}".format(remote, local)]

        final_cmd = ssh + ssh_options.to_ssh_options_list(timeout=timeout) + [
            "{}@{}".format(self.ssh_user, self.ssh_ip)
        ]
        if cmd:
            if environment_variables:
                cmd = _with_environment_variables(cmd, environment_variables)
            if is_using_login_shells():
                final_cmd += _with_interactive(cmd)
            else:
                final_cmd += [cmd]
        else:
            # We do this because `-o ControlMaster` causes the `-N` flag to
            # still create an interactive shell in some ssh versions.
            final_cmd.append("while true; do sleep 86400; done")

        cli_logger.verbose("Running `{}`", cf.bold(cmd))
        with cli_logger.indented():
            cli_logger.very_verbose("Full command is `{}`",
                                    cf.bold(" ".join(final_cmd)))

        if cli_logger.verbosity > 0:
            with cli_logger.indented():
                return self._run_helper(
                    final_cmd, with_output, exit_on_fail, silent=silent)
        else:
            return self._run_helper(
                final_cmd, with_output, exit_on_fail, silent=silent)

    def _create_rsync_filter_args(self, options):
        rsync_excludes = options.get("rsync_exclude") or []
        rsync_filters = options.get("rsync_filter") or []

        exclude_args = [["--exclude", rsync_exclude]
                        for rsync_exclude in rsync_excludes]
        filter_args = [["--filter", "dir-merge,- {}".format(rsync_filter)]
                       for rsync_filter in rsync_filters]

        # Combine and flatten the two lists
        return [
            arg for args_list in exclude_args + filter_args
            for arg in args_list
        ]

    def run_rsync_up(self, source, target, options=None):
        self._set_ssh_ip_if_required()
        options = options or {}

        command = ["rsync"]
        command += [
            "--rsh",
            subprocess.list2cmdline(
                ["ssh"] + self.ssh_options.to_ssh_options_list(timeout=120))
        ]
        command += ["-avz"]
        command += self._create_rsync_filter_args(options=options)
        command += [
            source, "{}@{}:{}".format(self.ssh_user, self.ssh_ip, target)
        ]
        cli_logger.verbose("Running `{}`", cf.bold(" ".join(command)))
        self._run_helper(command, silent=is_rsync_silent())

    def run_rsync_down(self, source, target, options=None):
        self._set_ssh_ip_if_required()

        command = ["rsync"]
        command += [
            "--rsh",
            subprocess.list2cmdline(
                ["ssh"] + self.ssh_options.to_ssh_options_list(timeout=120))
        ]
        command += ["-avz"]
        command += self._create_rsync_filter_args(options=options)
        command += [
            "{}@{}:{}".format(self.ssh_user, self.ssh_ip, source), target
        ]
        cli_logger.verbose("Running `{}`", cf.bold(" ".join(command)))
        self._run_helper(command, silent=is_rsync_silent())

    def remote_shell_command_str(self):
        if self.ssh_private_key:
            return "ssh -o IdentitiesOnly=yes -i {} {}@{}\n".format(
                self.ssh_private_key, self.ssh_user, self.ssh_ip)
        else:
            return "ssh -o IdentitiesOnly=yes {}@{}\n".format(
                self.ssh_user, self.ssh_ip)


class DockerCommandRunner(CommandRunnerInterface):
    def __init__(self, docker_config, **common_args):
        self.ssh_command_runner = SSHCommandRunner(**common_args)
        self.container_name = docker_config["container_name"]
        self.docker_config = docker_config
        self.home_dir = None
        self.initialized = False

    def run(
            self,
            cmd,
            timeout=120,
            exit_on_fail=False,
            port_forward=None,
            with_output=False,
            environment_variables: Dict[str, object] = None,
            run_env="auto",
            ssh_options_override_ssh_key="",
            shutdown_after_run=False,
    ):
        if run_env == "auto":
            run_env = "host" if cmd.find("docker") == 0 else "docker"

        if environment_variables:
            cmd = _with_environment_variables(cmd, environment_variables)

        if run_env == "docker":
            cmd = self._docker_expand_user(cmd, any_char=True)
            if is_using_login_shells():
                cmd = " ".join(_with_interactive(cmd))
            cmd = with_docker_exec(
                [cmd],
                container_name=self.container_name,
                with_interactive=is_using_login_shells())[0]

        if shutdown_after_run:
            # sudo shutdown should run after `with_docker_exec` command above
            cmd += "; sudo shutdown -h now"
        # Do not pass shutdown_after_run argument to ssh_command_runner.run()
        # since it is handled above.
        return self.ssh_command_runner.run(
            cmd,
            timeout=timeout,
            exit_on_fail=exit_on_fail,
            port_forward=port_forward,
            with_output=with_output,
            ssh_options_override_ssh_key=ssh_options_override_ssh_key)

    def run_rsync_up(self, source, target, options=None):
        options = options or {}
        host_destination = os.path.join(
            self._get_docker_host_mount_location(
                self.ssh_command_runner.cluster_name), target.lstrip("/"))

        self.ssh_command_runner.run(
            f"mkdir -p {os.path.dirname(host_destination.rstrip('/'))}",
            silent=is_rsync_silent())

        self.ssh_command_runner.run_rsync_up(
            source, host_destination, options=options)
        if self._check_container_status() and not options.get(
                "docker_mount_if_possible", False):
            if os.path.isdir(source):
                # Adding a "." means that docker copies the *contents*
                # Without it, docker copies the source *into* the target
                host_destination += "/."
            self.ssh_command_runner.run(
                "docker cp {} {}:{}".format(host_destination,
                                            self.container_name,
                                            self._docker_expand_user(target)),
                silent=is_rsync_silent())

    def run_rsync_down(self, source, target, options=None):
        options = options or {}
        host_source = os.path.join(
            self._get_docker_host_mount_location(
                self.ssh_command_runner.cluster_name), source.lstrip("/"))
        self.ssh_command_runner.run(
            f"mkdir -p {os.path.dirname(host_source.rstrip('/'))}",
            silent=is_rsync_silent())
        if source[-1] == "/":
            source += "."
            # Adding a "." means that docker copies the *contents*
            # Without it, docker copies the source *into* the target
        if not options.get("docker_mount_if_possible", False):
            self.ssh_command_runner.run(
                "docker cp {}:{} {}".format(self.container_name,
                                            self._docker_expand_user(source),
                                            host_source),
                silent=is_rsync_silent())
        self.ssh_command_runner.run_rsync_down(
            host_source, target, options=options)

    def remote_shell_command_str(self):
        inner_str = self.ssh_command_runner.remote_shell_command_str().replace(
            "ssh", "ssh -tt", 1).strip("\n")
        return inner_str + " docker exec -it {} /bin/bash\n".format(
            self.container_name)

    def _check_docker_installed(self):
        no_exist = "NoExist"
        output = self.ssh_command_runner.run(
            f"command -v docker || echo '{no_exist}'", with_output=True)
        cleaned_output = output.decode().strip()
        if no_exist in cleaned_output or "docker" not in cleaned_output:
            install_commands = [
                "curl -fsSL https://get.docker.com -o get-docker.sh",
                "sudo sh get-docker.sh", "sudo usermod -aG docker $USER",
                "sudo systemctl restart docker -f"
            ]
            logger.error(
                "Docker not installed. You can install Docker by adding the "
                "following commands to 'initialization_commands':\n" +
                "\n".join(install_commands))

    def _check_container_status(self):
        if self.initialized:
            return True
        output = self.ssh_command_runner.run(
            check_docker_running_cmd(self.container_name),
            with_output=True).decode("utf-8").strip()
        # Checks for the false positive where "true" is in the container name
        return ("true" in output.lower()
                and "no such object" not in output.lower())

    def _docker_expand_user(self, string, any_char=False):
        user_pos = string.find("~")
        if user_pos > -1:
            if self.home_dir is None:
                self.home_dir = self.ssh_command_runner.run(
                    f"docker exec {self.container_name} printenv HOME",
                    with_output=True).decode("utf-8").strip()

            if any_char:
                return string.replace("~/", self.home_dir + "/")

            elif not any_char and user_pos == 0:
                return string.replace("~", self.home_dir, 1)

        return string

    def _check_if_container_restart_is_needed(
            self, image: str, cleaned_bind_mounts: Dict[str, str]) -> bool:
        re_init_required = False
        running_image = self.run(
            check_docker_image(self.container_name),
            with_output=True,
            run_env="host").decode("utf-8").strip()
        if running_image != image:
            cli_logger.error(
                "A container with name {} is running image {} instead " +
                "of {} (which was provided in the YAML)", self.container_name,
                running_image, image)
        mounts = self.run(
            check_bind_mounts_cmd(self.container_name),
            with_output=True,
            run_env="host").decode("utf-8").strip()
        try:
            active_mounts = json.loads(mounts)
            active_remote_mounts = {
                mnt["Destination"].strip("/")
                for mnt in active_mounts
            }
            # Ignore ray bootstrap files.
            requested_remote_mounts = {
                self._docker_expand_user(remote).strip("/")
                for remote in cleaned_bind_mounts.keys()
            }
            unfulfilled_mounts = (
                requested_remote_mounts - active_remote_mounts)
            if unfulfilled_mounts:
                re_init_required = True
                cli_logger.warning(
                    "This Docker Container is already running. "
                    "Restarting the Docker container on "
                    "this node to pick up the following file_mounts {}",
                    unfulfilled_mounts)
        except json.JSONDecodeError:
            cli_logger.verbose(
                "Unable to check if file_mounts specified in the YAML "
                "differ from those on the running container.")
        return re_init_required

    def run_init(self, *, as_head, file_mounts, sync_run_yet):
        BOOTSTRAP_MOUNTS = [
            "~/ray_bootstrap_config.yaml", "~/ray_bootstrap_key.pem"
        ]

        specific_image = self.docker_config.get(
            f"{'head' if as_head else 'worker'}_image",
            self.docker_config.get("image"))

        self._check_docker_installed()
        if self.docker_config.get("pull_before_run", True):
            assert specific_image, "Image must be included in config if " + \
                "pull_before_run is specified"
            self.run("docker pull {}".format(specific_image), run_env="host")
        else:

            self.run(
                f"docker image inspect {specific_image} 1> /dev/null  2>&1 || "
                f"docker pull {specific_image}")

        # Bootstrap files cannot be bind mounted because docker opens the
        # underlying inode. When the file is switched, docker becomes outdated.
        cleaned_bind_mounts = file_mounts.copy()
        for mnt in BOOTSTRAP_MOUNTS:
            cleaned_bind_mounts.pop(mnt, None)

        docker_run_executed = False

        container_running = self._check_container_status()
        requires_re_init = False
        if container_running:
            requires_re_init = self._check_if_container_restart_is_needed(
                specific_image, cleaned_bind_mounts)
            if requires_re_init:
                self.run(f"docker stop {self.container_name}", run_env="host")

        if (not container_running) or requires_re_init:
            # Get home directory
            image_env = self.ssh_command_runner.run(
                "docker inspect -f '{{json .Config.Env}}' " + specific_image,
                with_output=True).decode().strip()
            home_directory = "/root"
            for env_var in json.loads(image_env):
                if env_var.startswith("HOME="):
                    home_directory = env_var.split("HOME=")[1]
                    break

            start_command = docker_start_cmds(
                self.ssh_command_runner.ssh_user, specific_image,
                cleaned_bind_mounts, self.container_name,
                self.docker_config.get(
                    "run_options", []) + self.docker_config.get(
                        f"{'head' if as_head else 'worker'}_run_options", []) +
                self._configure_runtime() + self._auto_configure_shm(),
                self.ssh_command_runner.cluster_name, home_directory)
            self.run(start_command, run_env="host")
            docker_run_executed = True

        # Explicitly copy in ray bootstrap files.
        for mount in BOOTSTRAP_MOUNTS:
            if mount in file_mounts:
                if not sync_run_yet:
                    # NOTE(ilr) This rsync is needed because when starting from
                    #  a stopped instance,  /tmp may be deleted and `run_init`
                    # is called before the first `file_sync` happens
                    self.run_rsync_up(file_mounts[mount], mount)
                self.ssh_command_runner.run(
                    "docker cp {src} {container}:{dst}".format(
                        src=os.path.join(
                            self._get_docker_host_mount_location(
                                self.ssh_command_runner.cluster_name), mount),
                        container=self.container_name,
                        dst=self._docker_expand_user(mount)))
        self.initialized = True
        return docker_run_executed

    def _configure_runtime(self):
        if self.docker_config.get("disable_automatic_runtime_detection"):
            return []

        runtime_output = self.ssh_command_runner.run(
            "docker info -f '{{.Runtimes}}' ",
            with_output=True).decode().strip()
        if "nvidia-container-runtime" in runtime_output:
            try:
                self.ssh_command_runner.run("nvidia-smi", with_output=False)
                return ["--runtime=nvidia"]
            except Exception as e:
                logger.warning(
                    "Nvidia Container Runtime is present, but no GPUs found.")
                logger.debug(f"nvidia-smi error: {e}")
                return []

        return []

    def _auto_configure_shm(self):
        if self.docker_config.get("disable_shm_size_detection"):
            return []
        try:
            shm_output = self.ssh_command_runner.run(
                "cat /proc/meminfo || true",
                with_output=True).decode().strip()
            available_memory = int(
                [ln for ln in shm_output.split("\n")
                 if "MemAvailable" in ln][0].split()[1])
            available_memory_bytes = available_memory * 1024
            # Overestimate SHM size by 10%
            shm_size = min((available_memory_bytes *
                            DEFAULT_OBJECT_STORE_MEMORY_PROPORTION * 1.1),
                           DEFAULT_OBJECT_STORE_MAX_MEMORY_BYTES)
            return [f"--shm-size='{shm_size}b'"]
        except Exception as e:
            logger.warning(
                f"Received error while trying to auto-compute SHM size {e}")
            return []

    def _get_docker_host_mount_location(self, cluster_name: str) -> str:
        """Return the docker host mount directory location."""
        # Imported here due to circular dependency in imports.
        from ray.autoscaler.sdk import get_docker_host_mount_location
        return get_docker_host_mount_location(cluster_name)
