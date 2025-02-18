#!/usr/bin/env python3
import datetime
import os
import signal
import sys
import traceback
from multiprocessing.context import Process

import cereal.messaging as messaging
import selfdrive.crash as crash
from common.params import Params
from common.text_window import TextWindow
from selfdrive.boardd.set_time import set_time
from selfdrive.hardware import HARDWARE, TICI
from selfdrive.manager.helpers import unblock_stdout
from selfdrive.manager.process import ensure_running, launcher
from selfdrive.manager.process_config import managed_processes
from selfdrive.registration import register
from selfdrive.swaglog import cloudlog, add_file_handler
from selfdrive.version import dirty, version
from selfdrive.hardware.eon.apk import system


def manager_init():

  # update system time from panda
  set_time(cloudlog)

  params = Params()
  params.manager_start()

  default_params = [
    ("CompletedTrainingVersion", "0"),
    ("HasAcceptedTerms", "0"),
    ("LastUpdateTime", datetime.datetime.utcnow().isoformat().encode('utf8')),
    ("OpenpilotEnabledToggle", "1"),
    ("IsMetric", "1"),

    # HKG
    ("UseClusterSpeed", "1"),
    ("LongControlEnabled", "0"),
    ("MadModeEnabled", "1"),
    ("AutoLaneChangeEnabled", "0"),

    ("SccSmootherSlowOnCurves", "0"),
    ("SccSmootherSyncGasPressed", "0"),
    ("ShowDebugUI", "0")
  ]

  if TICI:
    default_params.append(("IsUploadRawEnabled", "1"))

  if params.get_bool("RecordFrontLock"):
    params.put_bool("RecordFront", True)

  # set unset params
  for k, v in default_params:
    if params.get(k) is None:
      params.put(k, v)

  # is this dashcam?
  if os.getenv("PASSIVE") is not None:
    params.put_bool("Passive", bool(int(os.getenv("PASSIVE"))))

  if params.get("Passive") is None:
    raise Exception("Passive must be set to continue")

  os.umask(0)  # Make sure we can create files with 777 permissions

  # Create folders needed for msgq
  try:
    os.mkdir("/dev/shm")
  except FileExistsError:
    pass
  except PermissionError:
    print("WARNING: failed to make /dev/shm")

  # set dongle id
  reg_res = register(show_spinner=True)
  if reg_res:
    dongle_id = reg_res
  else:
    raise Exception("server registration failed")
  os.environ['DONGLE_ID'] = dongle_id  # Needed for swaglog and loggerd

  if not dirty:
    os.environ['CLEAN'] = '1'

  cloudlog.bind_global(dongle_id=dongle_id, version=version, dirty=dirty,
                       device=HARDWARE.get_device_type())
  crash.bind_user(id=dongle_id)
  crash.bind_extra(version=version, dirty=dirty, device=HARDWARE.get_device_type())


def manager_prepare():
  for p in managed_processes.values():
    p.prepare()


def manager_cleanup():
  for p in managed_processes.values():
    p.stop()

  cloudlog.info("everything is dead")


def manager_thread():

  Process(name="shutdownd", target=launcher, args=("selfdrive.shutdownd",)).start()
  system("am startservice com.neokii.optool/.MainService")
  system("am startservice com.neokii.openpilot/.MainService")

  cloudlog.info("manager start")
  cloudlog.info({"environ": os.environ})

  # save boot log
  #subprocess.call("./bootlog", cwd=os.path.join(BASEDIR, "selfdrive/loggerd"))

  ignore = []
  if os.getenv("NOBOARD") is not None:
    ignore.append("pandad")
  if os.getenv("BLOCK") is not None:
    ignore += os.getenv("BLOCK").split(",")

  ensure_running(managed_processes.values(), started=False, not_run=ignore)

  started_prev = False
  params = Params()
  sm = messaging.SubMaster(['deviceState'])
  pm = messaging.PubMaster(['managerState'])

  while True:
    sm.update()
    not_run = ignore[:]

    if sm['deviceState'].freeSpacePercent < 5:
      not_run.append("loggerd")

    started = sm['deviceState'].started
    driverview = params.get_bool("IsDriverViewEnabled")
    ensure_running(managed_processes.values(), started, driverview, not_run)

    # trigger an update after going offroad
    if started_prev and not started and 'updated' in managed_processes:
      os.sync()
      managed_processes['updated'].signal(signal.SIGHUP)

    started_prev = started

    running_list = ["%s%s\u001b[0m" % ("\u001b[32m" if p.proc.is_alive() else "\u001b[31m", p.name)
                    for p in managed_processes.values() if p.proc]
    cloudlog.debug(' '.join(running_list))

    # send managerState
    msg = messaging.new_message('managerState')
    msg.managerState.processes = [p.get_process_state_msg() for p in managed_processes.values()]
    pm.send('managerState', msg)

    # TODO: let UI handle this
    # Exit main loop when uninstall is needed
    if params.get_bool("DoUninstall"):
      break


def main():
  prepare_only = os.getenv("PREPAREONLY") is not None

  manager_init()

  # Start UI early so prepare can happen in the background
  if not prepare_only:
    managed_processes['ui'].start()

  manager_prepare()

  if prepare_only:
    return

  # SystemExit on sigterm
  signal.signal(signal.SIGTERM, lambda signum, frame: sys.exit(1))

  try:
    manager_thread()
  except Exception:
    traceback.print_exc()
    crash.capture_exception()
  finally:
    manager_cleanup()

  if Params().get_bool("DoUninstall"):
    cloudlog.warning("uninstalling")
    HARDWARE.uninstall()


if __name__ == "__main__":
  unblock_stdout()

  try:
    main()
  except Exception:
    add_file_handler(cloudlog)
    cloudlog.exception("Manager failed to start")

    # Show last 3 lines of traceback
    error = traceback.format_exc(-3)
    error = "Manager failed to start\n\n" + error
    with TextWindow(error) as t:
      t.wait_for_exit()

    raise

  # manual exit because we are forked
  sys.exit(0)
