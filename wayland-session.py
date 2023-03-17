#!/usr/bin/python

# Universal Wayland Desktop Session Manager
# Runs selected WM with plugin-extendable tweaks
# Manages systemd environment and targets along the way
# Inspired by and uses some techniques from:
#  https://github.com/xdbob/sway-services
#  https://github.com/alebastr/sway-systemd
#  https://github.com/swaywm/sway
#  https://people.debian.org/~mpitt/systemd.conf-2016-graphical-session.pdf

import os, sys, shlex, argparse, re, subprocess, textwrap, random, time, signal
from io import StringIO
from xdg.DesktopEntry import DesktopEntry, which
from xdg import BaseDirectory

class varnames:
    'Sets of varnames'
    always_export = {
        'XDG_SESSION_ID',
        'XDG_VTNR',
        'XDG_CURRENT_DESKTOP',
        'XDG_SESSION_DESKTOP',
        'XDG_MENU_PREFIX',
        'PATH'
    }
    never_export = {
        'PWD',
        'LS_COLORS',
        'INVOCATION_ID',
        'SHLVL',
        'SHELL'
    }
    always_unset = {
        'DISPLAY',
        'WAYLAND_DISPLAY'
    }
    always_cleanup = {
        'DISPLAY',
        'WAYLAND_DISPLAY',
        'XDG_SESSION_ID',
        'XDG_VTNR',
        'XDG_CURRENT_DESKTOP',
        'XDG_SESSION_DESKTOP',
        'XDG_MENU_PREFIX',
        'PATH',
        'XCURSOR_THEME',
        'XCURSOR_SIZE',
        'LANG'
    }
    never_cleanup = {
        'SSH_AGENT_LAUNCHER',
        'SSH_AUTH_SOCK',
        'SSH_AGENT_PID'
    }

def dedent(data):
    'Applies dedent, lstrips newlines, rstrips except single newline'
    data = textwrap.dedent(data).lstrip('\n')
    if data.endswith('\n'):
        return data.rstrip() + '\n'
    else:
        return data.rstrip()

class styles:
    reset = '\033[0m'
    red = '\033[31m'
    green = '\033[32m'
    yellow = '\033[33m'
    pale_yellow = '\033[97m'
    blue = '\033[34m'
    violet = '\033[35m'
    header = '\033[95m'
    bold = '\033[1m'
    under = '\033[4m'
    strike = '\033[9m'
    flash = '\033[5m'

def random_hex(length=16):
    'Returns random hex string of length'
    return ''.join(
        [random.choice(list('0123456789abcdef')) for i in range(0, length)]
    )

def sane_split(string, delimiter):
    'Splits string by delimiter, but returns empty list on empty string'
    if not isinstance(string, str):
        raise Exception(f"This is not a string: {string}")
    if not string:
        return []
    else:
        return string.split(delimiter)

# all print_* functions force flush for synchronized output
def print_normal(*args):
    print(*args, flush=True)

def print_ok(*args):
    print(styles.green, end='', flush=True)
    print(*args, flush=True)
    print(styles.reset, end='', file=sys.stderr, flush=True)

def print_warning(*args):
    print(styles.yellow, end='', flush=True)
    print(*args, flush=True)
    print(styles.reset, end='', file=sys.stderr, flush=True)

def print_error(*args):
    print(styles.red, end='', file=sys.stderr, flush=True)
    print(*args, file=sys.stderr, flush=True)
    print(styles.reset, end='', file=sys.stderr, flush=True)

def print_debug(*args):
    if int(os.getenv('DEBUG', '0')) > 0:
        print('DEBUG\n', *args, '\nEND_DEBUG', file=sys.stderr, flush=True)
        print(styles.reset, end='', file=sys.stderr, flush=True)

def print_style(stls, *args):
    'Prints selected style(s), then args, then resets'
    if isinstance(stls, string):
        stls = [stls]
    for style in stls:
        print(style, end='', flush=True)
    print(*args, flush=True)
    print(styles.reset, end='', file=sys.stderr, flush=True)

def relax_validation(exception):
    'takes pyxdg ValidationError and ignores [stupid] failures'
    ignore_lines = 'ValidationError in file| is not a registered |Invalid key: '
    msgs = str(exception).splitlines()
    for line in msgs:
        if not re.search(ignore_lines, line):
            return False
    return True

def self_name():
    'returns basename of argv[0]'
    return os.path.basename(sys.argv[0])

def self_path():
    path = which(self_name())
    if path:
        return path
    else:
        print_error(f"{self_name()} is not in PATH or is not executable")
        sys.exit(0)

def load_lib_paths(subpath):
    'get plugins in style of BaseDirectory.load_*_paths'
    out = []
    for path in reversed(os.getenv(
        'UWSM_PLUGIN_PREFIX_PATHS',
        f"{os.getenv('HOME')}/.local/lib:/usr/local/lib:/usr/lib:/lib"
    ).split(':')):
        file = os.path.join(path, subpath)
        if os.path.isfile(file):
            out.append(file)
    return out

def get_de(entry_name, subpath='wayland-sessions'):
    """
    "which" for desktop entries, takes entry ID or path and hierarchy subpath
    "wayland-sessions", "applications", etc..., default "wayland-sessions"
    returns DesktopEntry object or empty dict
    """

    if not entry_name.endswith('.desktop'):
        print_error(f"Invalid entry name \"{entry_name}\"")
        return {}

    # if absolute path, use it
    if os.path.isabs(entry_name):
        entry_path = entry_name
        print_debug(f"Entry path is absolute: \"{entry_name}\"")

    # or find highest priority entry in data hierarchy
    else:
        print_debug(f"Entry path is relative: \"{entry_name}\"\n searching in \"{list(BaseDirectory.load_data_paths(subpath))}\"")
        print_debug()
        entry_path = list(BaseDirectory.load_data_paths(os.path.normpath(os.path.join(subpath, entry_name))))
        if entry_path:
            entry_path = entry_path[0]
        else:
            entry_path = ''

    if not entry_path:
        return {}

    # parse entry
    try:
        entry = DesktopEntry(entry_path)
    except exception as E:
        print_error(E)
        return {}

    print_debug(entry)

    # inaccessible executables or hidden entries
    msg = ''
    if entry.getHidden():
        msg = 'Entry is hidden'
    if ((entry.getTryExec() and not entry.findTryExec())
        or not entry.getExec()
        or not which(shlex.split(entry.getExec())[0])
    ):
        msg = 'Entry is missing an executable'
    if msg:
        print_warning(msg)
        return {}
    else:
        return entry

def wrap_process(
    argv,
    stdin=None,
    stdout=subprocess.PIPE,
    stderr=subprocess.PIPE,
    text=True,
    env=None):
    'Takes argv, runs with subprocess.Popen, returns stdout, stderr, returncode'

    if isinstance(argv, str):
        argv = shlex.split(argv)
    if isinstance(stdin, str) and '\n' in stdin:
        stdin_arg = subprocess.PIPE
    else:
        stdin_arg = stdin

    sp = subprocess.Popen(
        argv,
        stdin=stdin_arg,
        stdout=stdout,
        stderr=stderr,
        env=env,
        text=text
    )
    if isinstance(stdin, str) and '\n' in stdin:
        stdout, stderr = sp.communicate(StringIO(stdin).read())
    else:
        stdout, stderr = sp.communicate()
    return (stdout, stderr, sp.returncode)

def get_default_de():
    'Gets WM desktop entry ID from wayland-session-default-id file in config hierarchy'
    for cmd_cache_file in BaseDirectory.load_config_paths('wayland-session-default-id'):
        if os.path.isfile(cmd_cache_file):
            try:
                with open(cmd_cache_file, 'r') as cmd_cache_file:
                    for line in cmd_cache_file.readlines():
                        if line.strip():
                            wmid = line.strip()
                            print_ok(f"Got default WM ID: {wmid}")
                            return wmid
            except exception as E:
                print_error(E)
                continue
    return ''

def save_default_de(default):
    'Gets saves WM desktop entry ID from wayland-session-default-id file in config hierarchy'
    if not args.dry_run:
        if not os.path.isdir(BaseDirectory.xdg_config_home):
            os.mkdir(BaseDirectory.xdg_config_home)
        config = os.path.join(BaseDirectory.xdg_config_home, 'wayland-session-default-id')
        with open(config, 'w') as config:
            config.write(default)
            print_ok(f"Saved default WM ID: {default}")
    else:
        print_ok(f"Would save default WM ID: {default}")

def select_de():
    'Uses whiptail to select among "wayland-sessions" desktop entries'

    default = get_default_de()
    seen_entries = []
    entry_files = []
    choices_raw = []
    choices = []

    # find relevant desktop entries as first found paths in data hierarchy
    for dir in BaseDirectory.load_data_paths('wayland-sessions'):
        if os.path.isdir(dir):
            try:
                os.chdir(dir)
                for file in os.listdir():
                    if os.path.splitext(file)[1] == '.desktop' and file not in seen_entries:
                        seen_entries.append(file)
                        entry_files.append(os.path.join(dir, file))
            except Exception as E:
                print_error(E)
                sys.exit(126)

    entry_files.sort()

    # fill choces list with [Exec, Name], filtered
    for entry in entry_files:
        try:
            entry = DesktopEntry(entry)
        except:
            # just skip unparsable entry
            continue
        # skip inaccessible executables or hidden entries
        if (
            entry.getHidden()
            or entry.getNoDisplay()
            or (entry.getTryExec() and not entry.findTryExec())
            or not entry.getExec()
            or not which(shlex.split(entry.getExec())[0])
        ):
            continue
        name = entry.getName() or os.path.splitext(os.path.basename(entry.filename))[0]
        generic_name = entry.get('GenericName', '')
        comment = entry.get('Comment', '')
        description = f"{name}, {generic_name}" if generic_name else name
        # add a choice
        choices_raw.append([os.path.basename(entry.filename), description, comment])

    # pretty format choices
    description_length = 0
    for choice in choices_raw:
        if len(choice[1]) > description_length:
            description_length = len(choice[1])
    for choice in choices_raw:
        choices.append(choice[0])
        if choice[2]:
            choices.append('{} ({})'.format(
                choice[1].ljust(description_length),
                choice[2]
            ))
        else:
            choices.append(choice[1])

    if len(choices) == 2 and default == choices[0]:
        # just spit out the only preselected choice
        raise Exception(f"Choices for whiptail not even: {choices}")
    elif len(choices) > 2 and len(choices) % 2 == 0:
        # drop default if not among choices
        if default and default not in choices[::2]:
            default = ''
        elif default and args.wm == 'default':
            # just spit out default
            for choice in choices[::2]:
                if choice == default:
                    return choice
    elif len(choices) == 0:
        raise Exception('No choices found')
    else:
        raise Exception('Malformed choices')

    # generate arguments for whiptail exec
    argv = (
        'whiptail',
        '--clear',
        '--backtitle',
        'Universal Wayland Session Manager',
        '--title',
        'Choose compositor',
        '--nocancel',
        *(
            ('--default-item', default) if default else ()
        ),
        '--menu',
        '',
        '0',
        '0',
        '0',
        '--notags',
        *(choices)
    )

    # replace whiptail theme with simple default colors
    whiptail_env = dict(os.environ)
    whiptail_env.update({
        'TERM': 'linux',
        'NEWT_COLORS': ';'.join([
            'root=default,default',
            'border=default,default',
            'window=default,default',
            'shadow=default,default',
            'title=black,lightgray',
            'button=default,default',
            'actbutton=black,lightgray',
            'compactbutton=default,default',
            'checkbox=default,default',
            'actcheckbox=black,lightgray',
            'entry=default,default',
            'disentry=default,default',
            'label=default,default',
            'listbox=default,default',
            'actlistbox=black,lightgray',
            'sellistbox=black,lightgray',
            'actsellistbox=black,lightgray',
            'textbox=default,default',
            'acttextbox=black,lightgray',
            'emptyscale=default,default',
            'fullscale=default,default',
            'helpline=default,default',
            'roottext=default,default'
        ])
    })

    # run whiptail, capture stderr
    stdout, stderr, returncode = wrap_process(
        argv,
        env=whiptail_env,
        stdout=sys.stdout
    )

    if returncode == 0 and stderr:
        return stderr
    else:
        return ''

def get_unit_path(unit, category='runtime', level='user'):
    'Returns tuple: 0) path in category, level dir, 1) unit subpath'
    if os.path.isabs(unit):
        raise Exception("Passed absolute path to get_unit_path")

    unit = os.path.normpath(unit)

    unit_path = ''
    if category == 'runtime':
        try:
            unit_path = BaseDirectory.get_runtime_dir(strict=True)
        except:
            pass
        if not unit_path:
            print_error('Fatal: empty or undefined XDG_RUNTIME_DIR')
            sys.exit(0)
    else:
        raise Exception(f"category {category} is not supported")

    if level not in ['user']:
        raise Exception(f"level {level} is not supported")

    unit_dir = os.path.normpath(os.path.join(unit_path, 'systemd', level))
    return (unit_dir, unit)

def get_active_wm_id():
    'finds running wayland-wm@*.service, returns specifier'
    stdout, stderr, returncode = wrap_process([
        'systemctl',
        '--user',
        'show',
        '--state=active,activating',
        '--property',
        'Id',
        '--value',
        'wayland-wm@*.service'
    ])
    if not stdout.strip() or returncode != 0:
        raise Exception(stdout + stderr)
    active_id = stdout.strip()
    stdout, stderr, returncode = wrap_process([
        'systemd-escape',
        '--unescape',
        '--instance',
        active_id
    ])
    if not stdout.strip() or returncode != 0:
        raise Exception(stdout + stderr)
    return stdout.strip()

def is_active(check_wm_id='', verbose=False):
    'Checks if generic or specific wm_id is in active or activating state, returns bool'
    if check_wm_id:
        check_unit = f"wayland-wm@{check_wm_id}.service"
    else:
        check_unit = f"wayland-wm@*.service"
    check_unit_generic = f"wayland-wm@*.service"
    stdout, stderr, returncode = wrap_process([
        'systemctl',
        '--user',
        'list-units',
        '--state=active,activating',
        '-q',
        '--full',
        '--plain',
        check_unit
    ])
    print_debug(f"is-active stdout:\n{stdout.strip()}\nis-active stderr:\n{stderr.strip()}")
    if returncode != 0:
        # list-units always returns 0
        raise_exception(stdout + '\n' + stderr)
    elif stdout.strip():
        if verbose:
            if stdout.strip(): print_normal(stdout.strip())
            if stderr.strip(): print_error(stderr.strip())
        return True
    else:
        if verbose:
            stdout, stderr, returncode = wrap_process([
                'systemctl',
                '--user',
                'list-units',
                '--all',
                '-q',
                '--full',
                '--plain',
                check_unit_generic
            ])
            # don't bother for returncode here
            if stdout.strip(): print_normal(stdout.strip())
            if stderr.strip(): print_error(stderr.strip())
        return False

def reload_systemd():
    'Reloads systemd user manager'

    global units_changed
    if args.dry_run:
        print_normal('Will reload systemd user manager')
        units_changed = False
        return True

    print_normal('Reloading systemd user manager')
    stdout, stderr, returncode = wrap_process([
        'systemctl',
        '--user',
        'daemon-reload',
        '-q'
    ])
    if stdout.strip(): print_normal(stdout.strip())
    if stderr.strip(): print_error(stderr.strip())
    if returncode != 0:
        raise Exception(f"\"systemctl --user daemon-reload -q\" returned {returncode}")
    else:
        units_changed = False
        return(True)

def update_unit(unit, data):
    'Updates unit with data if differs'
    'Returns change in boolean'

    global units_changed

    if not re.search(r'[a-zA-Z0-9_:.\\-]+@?\.(service|slice|scope|target|d/[a-zA-Z0-9_:.\\-]+.conf)$', unit):
        raise Exception(f"Trying to update unit with unsupported extension {unit.split('.')[-1]}: {unit}")

    if os.path.isabs(unit):
        unit_dir, unit = ('/', os.path.normpath(unit))
    else:
        if unit.count('/') > 1:
            raise Exception(f"Only single subdir supported for relative unit, got {unit.count('/')} ({unit})")
        unit_dir, unit = get_unit_path(unit)
    unit_path = os.path.join(unit_dir, unit)

    # create subdirs if missing
    check_dir = unit_dir
    if not os.path.isdir(check_dir):
        if not args.dry_run:
            os.mkdir(check_dir)
            print_ok(f"Created dir \"{check_dir}/\"")
        elif not units_changed:
            print_ok(f"Will create dir \"{check_dir}/\"")
    for dir in [d for d in os.path.dirname(unit).split(os.path.sep) if d]:
        check_dir = os.path.join(check_dir, dir)
        if not os.path.isdir(check_dir):
            if not args.dry_run:
                os.mkdir(check_dir)
                print_ok(f"Created unit subdir \"{dir}/\"")
            else:
                print_ok(f"Will create unit subdir \"{dir}/\"")

    old_data = ''
    if os.path.isfile(unit_path):
        with open(unit_path, 'r') as unit_file:
            old_data = unit_file.read()

    if data == old_data:
        return False
    else:
        if not args.dry_run:
            with open(unit_path, 'w') as unit_file:
                unit_file.write(data)
            print_ok(f"Updated \"{unit}\"")
        else:
            print_ok(f"Will update \"{unit}\"")
        units_changed = True
        return True

def remove_unit(unit):
    'Removes unit and subdir if empty'

    global units_changed

    if not re.search(r'[a-zA-Z0-9_:.\\-]+@?\.(service|slice|scope|target|d/[a-zA-Z0-9_:.\\-]+.conf)$', unit):
        raise Exception(f"Trying to remove unit with unsupported extension {unit.split('.')[-1]}")

    if os.path.isabs(unit):
        unit_dir, unit = ('/', os.path.normpath(unit))
    else:
        if unit.count('/') > 1:
            raise Exception(f"Only single subdir supported for relative unit, got {unit.count('/')} ({unit})")
        unit_dir, unit = get_unit_path(unit)
    unit_path = os.path.join(unit_dir, unit)

    change = False
    # remove unit file
    if os.path.isfile(unit_path):
        if not args.dry_run:
            os.remove(unit_path)
            print_ok(f"Removed unit {unit}")
        else:
            print_ok(f"Will remove unit {unit}")
        units_changed = True
        change = True

    # deal with subdir
    if not os.path.isabs(unit) and '/' in unit:
        unit_subdir_path = os.path.dirname(unit_path)
        unit_subdir = os.path.dirname(unit)
        unit_filename = os.path.basename(unit_path)
        if os.path.isdir(unit_subdir_path):
            if set(os.listdir(unit_subdir_path)) - {unit_filename}:
                print_warning(f"Subdir {subdirunit_subdir_path} is not empty")
            else:
                if not args.dry_run:
                    os.rmdir(unit_subdir_path)
                    print_ok(f"Removed unit subdir {unit_subdir}")
                else:
                    print_ok(f"Will remove unit subdir {unit_subdir}")

    return(change)

def generate_units():
    'Generates basic unit structure'

    ws_self = self_name()
    ws_self_full = self_path()
    if not ws_self_full:
        print_error(f"{ws_self} is not in PATH. Can not continue!")
        sys.exit(1)

    if os.getenv('UWSM_USE_SESSION_SLICE', 'false') == 'true':
        wayland_wm_slice = 'session.slice'
    else:
        wayland_wm_slice = 'app.slice'

    global units_changed
    units_changed = False

    # targets
    update_unit(
        "wayland-session-pre@.target",
        dedent(f"""
            # injected by {ws_self}, do not edit
            [Unit]
            X-UWSMType=generic
            Description=Preparation for session of %I Wayland Window Manager
            Documentation=man:systemd.special(7)
            Requires=basic.target
            StopWhenUnneeded=yes
            BindsTo=graphical-session-pre.target
            Before=graphical-session-pre.target
            PropagatesStopTo=graphical-session-pre.target
        """)
    )
    update_unit(
        "wayland-session@.target",
        dedent(f"""
            # injected by {ws_self}, do not edit
            [Unit]
            X-UWSMType=generic
            Description=Session of %I Wayland Window Manager
            Documentation=man:systemd.special(7)
            Requires=wayland-session-pre@%i.target graphical-session-pre.target
            After=wayland-session-pre@%i.target graphical-session-pre.target
            StopWhenUnneeded=yes
            BindsTo=graphical-session.target
            Before=graphical-session.target
            PropagatesStopTo=graphical-session.target
        """)
    )
    update_unit(
        "wayland-session-xdg-autostart@.target",
        dedent(f"""
            # injected by {ws_self}, do not edit
            [Unit]
            X-UWSMType=generic
            Description=XDG Autostart for session of %I Wayland Window Manager
            Documentation=man:systemd.special(7)
            Requires=wayland-session@%i.target graphical-session.target
            After=wayland-session@%i.target graphical-session.target
            StopWhenUnneeded=yes
            BindsTo=xdg-desktop-autostart.target
            Before=xdg-desktop-autostart.target
            PropagatesStopTo=xdg-desktop-autostart.target
        """)
    )

    # services
    update_unit(
        "wayland-wm-env@.service",
        dedent(f"""
            # injected by {ws_self}, do not edit
            [Unit]
            X-UWSMType=generic
            Description=Environment preloader for %I Wayland Window Manager
            Documentation=man:systemd.service(7)
            BindsTo=wayland-session-pre@%i.target
            Before=wayland-session-pre@%i.target
            StopWhenUnneeded=yes
            [Service]
            Type=oneshot
            RemainAfterExit=yes
            ExecStart={ws_self_full} aux prepare-env "%I"
            ExecStop={ws_self_full} aux cleanup-env
            Restart=no
            Slice={wayland_wm_slice}
        """)
    )
    update_unit(
        "wayland-wm@.service",
        dedent(f"""
            # injected by {ws_self}, do not edit
            [Unit]
            X-UWSMType=generic
            Description=%I Wayland Window Manager
            Documentation=man:systemd.service(7)
            BindsTo=wayland-session@%i.target
            Before=wayland-session@%i.target
            Requires=wayland-wm-env@%i.service graphical-session-pre.target
            After=wayland-wm-env@%i.service graphical-session-pre.target
            Wants=wayland-session-xdg-autostart@%i.target xdg-desktop-autostart.target
            Before=wayland-session-xdg-autostart@%i.target xdg-desktop-autostart.target app-graphical.slice background-graphical.slice session-graphical.slice
            PropagatesStopTo=app-graphical.slice background-graphical.slice session-graphical.slice
            # dirty fix of xdg-desktop-portal-gtk.service shudown
            PropagatesStopTo=xdg-desktop-portal-gtk.service
            [Service]
            # awaits for 'systemd-notify --ready' from WM child
            Type=notify
            NotifyAccess=all
            ExecStart={ws_self_full} aux exec %I
            Restart=no
            TimeoutStartSec=10
            TimeoutStopSec=10
            Slice={wayland_wm_slice}
        """)
    )

    # slices
    update_unit(
        "app-graphical.slice",
        dedent(f"""
            # injected by {ws_self}, do not edit
            [Unit]
            X-UWSMType=generic
            Description=User Graphical Application Slice
            Documentation=man:systemd.special(7)
            PartOf=graphical-session.target
            After=graphical-session.target
            """)
    )
    update_unit(
        "background-graphical.slice",
        dedent(f"""
            # injected by {ws_self}, do not edit
            [Unit]
            X-UWSMType=generic
            Description=User Graphical Background Application Slice
            Documentation=man:systemd.special(7)
            PartOf=graphical-session.target
            After=graphical-session.target
        """)
    )
    update_unit(
        "session-graphical.slice",
        dedent(f"""
            # injected by {ws_self}, do not edit
            [Unit]
            X-UWSMType=generic
            Description=User Graphical Session Application Slice
            Documentation=man:systemd.special(7)
            PartOf=graphical-session.target
            After=graphical-session.target
        """)
    )

    # WM-specific cli-given additions via drop-ins
    wm_specific_preloader = []
    wm_specific_service = []

    if wm_cli_argv or wm_cli_desktop_names or wm_cli_name or wm_cli_description:
        wm_specific_preloader.append(dedent(f"""
            # injected by {ws_self}, do not edit
            [Unit]
            X-UWSMType={wm_id}
        """))
        wm_specific_service.append(dedent(f"""
            # injected by {ws_self}, do not edit
            [Unit]
            X-UWSMType={wm_id}
        """))

        if wm_cli_name:
            wm_specific_preloader.append(dedent("""
                Description=Environment preloader for {wm_cli_name}
            """))

        if wm_cli_description:
            wm_specific_service.append(dedent(f"""
                Description={wm_cli_name or wm_id}, {wm_cli_description}
            """))
        elif wm_cli_name:
            wm_specific_service.append(dedent(f"""
                Description={wm_cli_name} Wayland Window Manager
            """))

        if wm_cli_desktop_names or wm_cli_argv:
            if wm_cli_desktop_names:
                prepend = f" -D {':'.join(wm_cli_desktop_names)}"
            else:
                prepend = ''
            if wm_cli_argv:
                append = f" {shlex.join(wm_cli_argv)}"
            else:
                append = ''

            wm_specific_preloader.append(dedent(f"""
                [Service]
                ExecStart=
                ExecStart={ws_self_full} aux prepare-env{prepend} "%I"{append}
            """))
            if append:
                wm_specific_service.append(dedent(f"""
                    [Service]
                    ExecStart=
                    ExecStart={ws_self_full} aux exec "%I"{append}
                """))

        update_unit(
            f"wayland-wm-env@{wm_id}.service.d/custom.conf",
            '\n'.join(wm_specific_preloader)
        )
        update_unit(
            f"wayland-wm@{wm_id}.service.d/custom.conf",
            '\n'.join(wm_specific_service)
        )
    else:
        # remove customization tweaks
        remove_unit(f"wayland-wm-env@{wm_id}.service.d/custom.conf")
        remove_unit(f"wayland-wm@{wm_id}.service.d/custom.conf")

    # tweaks
    update_unit(
        "app-@autostart.service.d/slice-tweak.conf",
        dedent(f"""
            # injected by {ws_self}, do not edit
            [Unit]
            # make autostart apps stoppable by target
            #StopPropagatedFrom=xdg-desktop-autostart.target
            PartOf=xdg-desktop-autostart.target
            X-UWSMType=generic
            [Service]
            # also put them in special graphical app slice
            Slice=app-graphical.slice
        """)
    )
    # this does not work
    #update_unit(
    #    "xdg-desktop-portal-gtk.service.d/part-tweak.conf",
    #    dedent(f"""
    #        # injected by {ws_self}, do not edit
    #        [Unit]
    #        # make the same thing as -wlr portal to stop correctly
    #        PartOf=graphical-session.target
    #        After=graphical-session.target
    #        ConditionEnvironment=WAYLAND_DISPLAY
    #        X-UWSMType=generic
    #    """)
    #)
    # this breaks xdg-desktop-portal-rewrite-launchers.service
    #update_unit(
    #    "xdg-desktop-portal-.service.d/slice-tweak.conf",
    #    dedent(f"""
    #        # injected by {ws_self}, do not edit
    #        [Service]
    #        # make xdg-desktop-portal-*.service implementations part of graphical scope
    #        Slice=app-graphical.slice
    #        X-UWSMType=generic
    #    """)
    #)

def remove_units(only=None):
    """
    Removes units by X-UWSMType= attribute.
    if wm_id is given as argument, only remove X-UWSMType={wm_id}, else remove all.
    """
    if not only:
        only = ''
    check_dir, dot = get_unit_path('')
    stdout, stderr, returncode = wrap_process([
        'grep',
        '-rlF',
        f"X-UWSMType={only}",
        check_dir
    ])
    files = stdout.splitlines()
    if returncode != 0 or not files:
        return
    files = [f.removeprefix(check_dir.rstrip('/') + '/') for f in files]
    for file in sorted(files):
        remove_unit(file)
    return

def parse_args():
    'Parses args, returns tuple with args and a dict of parsers'

    # keep parsers in a dict
    parsers = dict()

    # main parser with subcommands
    parsers['main'] = argparse.ArgumentParser(
        description = 'Universal Wayland Session Manager',
        #usage='%(prog)s [-h] action ...',
        # TODO: add epilog
        epilog = dedent("""
           See action -h|--help.
        """)
    )
    parsers['main_subparsers'] = parsers['main'].add_subparsers(
        title='Subcommands',
        description=None,
        dest='mode',
        metavar='action',
        required=True
    )

    # wm arguments for potential reuse via parents
    parsers['wm_args'] = argparse.ArgumentParser(add_help=False)
    parsers['wm_args'].add_argument(
        'wm',
        metavar='wm|wm.desktop',
        help='executable or desktop entry (used as WM ID)'
    )
    parsers['wm_args'].add_argument(
        'args',
        metavar='...',
        nargs=argparse.REMAINDER,
        help='any additional arguments'
    )
    parsers['wm_args'].add_argument(
        '-D',
        metavar='name[:name...]',
        dest='desktop_names',
        default='',
        help='names for XDG_CURRENT_DESKTOP (:-separated)'
    )
    parsers['wm_args'].add_argument(
        '-e',
        dest='desktop_names_exclusive',
        action='store_true',
        help='use desktop names from -d exclusively, discard other sources'
    )
    parsers['wm_args'].add_argument(
        '-N',
        metavar='Name',
        dest='wm_name',
        default='',
        help='Fancy name for WM (filled from desktop entry by default)'
    )
    parsers['wm_args'].add_argument(
        '-C',
        metavar='Comment',
        dest='wm_comment',
        default='',
        help='Fancy description for WM (filled from desktop entry by default)'
    )

    # start subcommand
    parsers['start'] = parsers['main_subparsers'].add_parser(
        'start',
        help='Start WM',
        description="Generates units for given WM command line or desktop entry and starts WM.",
        parents=[parsers['wm_args']]
    )
    parsers['start'].add_argument(
        '-o',
        action='store_true',
        dest='only_generate',
        help='only generate units, but do not start'
    )
    parsers['start'].add_argument(
        '-n',
        action='store_true',
        dest='dry_run',
        help='do not write or start anything'
    )

    # stop subcommand
    parsers['stop'] = parsers['main_subparsers'].add_parser(
        'stop',
        help='Stop WM',
        description='Stops WM and optionally removes generated units.'
    )
    #parsers['stop'].add_argument(
    #    'wm',
    #    nargs='?',
    #    metavar='wm|wm.desktop',
    #    help='executable or desktop entry (used as WM ID)'
    #)
    parsers['stop'].add_argument(
        '-r',
        nargs='?',
        metavar='wm|wm.desktop',
        default=False,
        dest='remove_units',
        help='also remove units (all or only wm-specific)'
    )
    parsers['stop'].add_argument(
        '-n',
        action='store_true',
        dest='dry_run',
        help='do not write or start anything'
    )

    # finalize subcommand
    parsers['finalize'] = parsers['main_subparsers'].add_parser(
        'finalize',
        help='Signal WM startup and export variables',
        description='For use inside WM. Sends startup notification to systemd user manager. Exports WAYLAND_DISPLAY, DISPLAY, and any optional variables to systemd user manager.'
    )
    parsers['finalize'].add_argument(
        'env_names',
        metavar='[ENV_NAME [ENV2_NAME ...]]',
        nargs='*',
        help='additional vars to export'
    )

    # app subcommand
    parsers['app'] = parsers['main_subparsers'].add_parser(
        'app',
        help='Scoped app launcher',
        description='Launches application as a scope in specific slice.'
    )
    parsers['app'].add_argument(
        'cmd',
        #metavar='cmd|app.desktop',
        metavar='cmd',
        #help='executable or desktop entry'
        help='executable'
    )
    parsers['app'].add_argument(
        'args',
        metavar='...',
        nargs=argparse.REMAINDER,
        help='arguments'
    )
    parsers['app'].add_argument(
        '-s',
        dest='slice',
        metavar='a|b|s|custom.slice',
        help=f"{{{styles.under}a{styles.reset}pp,{styles.under}b{styles.reset}ackground,{styles.under}s{styles.reset}ession}}-graphical.slice, or any other. (default: %(default)s)",
        default='a'
    )
    parsers['app'].add_argument(
        '-u',
        dest='unit_name',
        metavar='unit_name',
        help='override autogenerated unit name',
        default=''
    )

    # check subcommand
    parsers['check'] = parsers['main_subparsers'].add_parser(
        'check',
        help='Checkers of states',
        description="Performs a check, returns 0 if true, 1 if false.",
    )
    parsers['check_subparsers'] = parsers['check'].add_subparsers(
        title='Subcommands',
        description=None,
        dest='checker',
        metavar='checker',
        required=True
    )
    parsers['is_active'] = parsers['check_subparsers'].add_parser(
        'is-active',
        help='checks for active WM',
        description='Checks for any or specific WM in active or activating state'
    )
    parsers['is_active'].add_argument(
        'wm',
        nargs='?',
        help='specify WM by executable or desktop entry (without arguments)'
    )
    parsers['is_active'].add_argument(
        '-v',
        action='store_true',
        dest='verbose',
        help='show additional info'
    )

    parsers['may_start'] = parsers['check_subparsers'].add_parser(
        'may-start',
        help='checks for start conditions',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        description='Checks whether it is OK to launch a wayland session.',
        epilog=dedent("""
                Conditions:
                  Running from login shell
                  No wayland session is running
                  System is at graphical.target
                  Foreground VT is among allowed (default: 1)
                Arguments:
                  VTNR: allowed VT numbers to override default 1
        """)
    )
    parsers['may_start'].add_argument(
        'vtnr',
        metavar='[N [N] ...]',
        type=int,
        # default does not work here
        default=[1],
        nargs=argparse.REMAINDER,
        help='VT numbers allowed for start (default: %(default)s)'
    )
    parsers['may_start'].add_argument(
        '-v',
        action='store_true',
        dest='verbose',
        help='show info'
    )

    # aux subcommand
    parsers['aux'] = parsers['main_subparsers'].add_parser(
        'aux',
        help='Auxillary functions',
        description='Can only be called by systemd user manager as a unit',
    )
    parsers['aux_subparsers'] = parsers['aux'].add_subparsers(
        title='Subcommands',
        description=None,
        dest='aux_action',
        metavar='prepare-env|cleanup-env|exec',
        required=True
    )
    parsers['prepare_env'] = parsers['aux_subparsers'].add_parser(
        'prepare-env',
        help='prepares environment (for use in wayland-wm-env@.service in wayland-session-pre@.target)',
        description='Used in ExecStart of wayland-wm-env@.service.',
        parents=[parsers['wm_args']]
    )
    parsers['cleanup_env'] = parsers['aux_subparsers'].add_parser(
        'cleanup-env',
        help='Cleans up environment (for use in wayland-wm-env@.service in wayland-session-pre@.target)',
        description='Used in ExecStop of wayland-wm-env@.service.'
    )
    parsers['exec'] = parsers['aux_subparsers'].add_parser(
        'exec',
        help='Executes binary with arguments or desktop entry (for use in wayland-wm@.service in wayland-session@.target)',
        description='Used in ExecStart of wayland-wm@.service.'
    )
    parsers['exec'].add_argument(
        'wm',
        metavar='wm|wm.desktop',
        help='executable or desktop entry (used as WM ID)'
    )
    parsers['exec'].add_argument(
        'args',
        metavar='...',
        nargs=argparse.REMAINDER,
        help='any additional arguments'
    )

    args = parsers['main'].parse_args()
    return (args, parsers)

def finalize(additional_vars=[]):
    'Optionally takes list of additional vars. Exports defined subset of WAYLAND_DISPLAY, DISPLAY, additional vars.'
    if not os.getenv('WAYLAND_DISPLAY', ''):
        print_error('WAYLAND_DISPLAY is not defined or empty. Are we being run by a wayland compositor or not?')
        sys.exit(1)
    export_vars = []
    for var in ['WAYLAND_DISPLAY', 'DISPLAY'] + sorted(additional_vars):
        if os.getenv(var, None) != None and var not in export_vars:
            export_vars.append(var)

    wm_id = get_active_wm_id()

    # append vars to cleanup file
    cleanup_file = os.path.join(BaseDirectory.get_runtime_dir(strict=True), f"env_names_for_cleanup_{wm_id}")
    if os.path.isfile(cleanup_file):
        with open(cleanup_file, 'r') as open_cleanup_file:
            current_cleanup_varnames = {l.strip() for l in open_cleanup_file.readlines() if l.strip()}
    else:
        print_error(f"\"{cleanup_file}\" does not exist\nAssuming env preloader failed")
        sys.exit(1)
    with open(cleanup_file, 'w') as open_cleanup_file:
        open_cleanup_file.write('\n'.join(sorted(current_cleanup_varnames | set(export_vars))))

    # export vars
    print_normal(f"Exporting variables to systemd_user_manager:\n  " + '\n  '.join(export_vars))
    stdout, stderr, returncode = wrap_process([
        'dbus-update-activation-environment',
        '--systemd',
        *(export_vars)
    ])
    if stdout.strip(): print_normal(stdout.strip())
    if stderr.strip(): print_error(stderr.strip())
    if returncode != 0:
        sys.exit(1)
    stdout, stderr, returncode = wrap_process([
        'systemctl',
        '--user',
        'import-environment',
        *(export_vars)
    ])
    if stdout.strip(): print_normal(stdout.strip())
    if stderr.strip(): print_error(stderr.strip())
    if returncode != 0:
        sys.exit(1)

    # if no prior failures, exec systemd-notify
    print_normal(f"Finalizing startup of {wm_id}")
    os.execlp(
        'systemd-notify',
        'systemd-notify',
        '--ready'
    )

    # we should not be here
    print_error('Something went wrong')
    sys.exit(1)

def get_systemd_varnames():
    'Returns set of env names from systemd user manager'
    stdout, stderr, returncode = wrap_process([
        'systemctl',
        '--user',
        'show-environment'
    ])
    if returncode != 0:
        raise Exception(f"\"systemctl --user show-environment\" returned {returncode}")
    # Systemctl always returns one value per line
    # Either var=value or var=$'complex\nescaped\nvalue'.
    # Seems to be safe to use .splitlines().
    # If values are needed, they can be rendered
    return set([v.split('=')[0] for v in stdout.splitlines()])

def prepare_env_gen_sh(random_mark):
    """
    Takes a known random string, returns string with shell code for sourcing env.
    Code echoes given string to mark the beginning of "env -0" output
    """

    # vars for use in plugins
    shell_definitions = dedent(f"""
        __WM_ID__={shlex.quote(wm_id)}
        __WM_BIN_ID__={shlex.quote(wm_bin_id)}
        __WM_DESKTOP_NAMES__={shlex.quote(':'.join(wm_desktop_names))}
        __WM_FIRST_DESKTOP_NAME__={shlex.quote(wm_desktop_names[0])}
    """)

    # bake plugin load into shell
    shell_plugins = load_lib_paths(f"wayland-session-plugins/{wm_bin_id}.sh.in")
    shell_plugins_load = []
    for plugin in shell_plugins:
        shell_plugins_load.append(dedent(f"""
            echo "Loading plugin \\"{plugin}\\""
            . "{plugin}"
        """))
    shell_plugins_load = '\n'.join(shell_plugins_load)

    # static part
    shell_main_body = dedent("""
        load_config_env() {
        	#### iterate config dirs in increasing importance and source additional env from relative path in $1
        	__ALL_XDG_CONFIG_DIRS_REV__=''
        	__CONFIG_DIR__=''
        	OIFS="$IFS"
        	IFS=":"
        	for __CONFIG_DIR__ in ${XDG_CONFIG_HOME}:${XDG_CONFIG_DIRS}
        	do
        		IFS="$OIFS"
        		# fill list in reverse order
        		if [ -n "${__CONFIG_DIR__}" ]
        		then
        			__ALL_XDG_CONFIG_DIRS_REV__="${__ALL_XDG_CONFIG_DIRS_REV__}${__ALL_XDG_CONFIG_DIRS_REV__:+:}${__CONFIG_DIR__}"
        		fi
        	done
        	IFS=":"
        	for __CONFIG_DIR__ in ${__ALL_XDG_CONFIG_DIRS_REV__}
        	do
        		IFS="$OIFS"
        		if [ -r "${__CONFIG_DIR__}/${1}" ]
        		then
        			echo "Loading environment from ${__CONFIG_DIR__}/${1}"
        			#set -a
        			. "${__CONFIG_DIR__}/${1}"
        			#set +a
        		fi
        	done
        	IFS="$OIFS"
        	unset __CONFIG_DIR__
        	unset __ALL_XDG_CONFIG_DIRS_REV__
        	return 0
        }

        load_common_env() {
        	load_config_env "wayland-session-env"
        }

        load_wm_env() {
        	load_config_env "wayland-session-${__WM_BIN_ID__}-env"
        }

        #### Basic environment
        [ -f /etc/profile ] && . /etc/profile
        [ -f "${HOME}/.profile" ] && . "${HOME}/.profile"
        export PATH
        export XDG_CONFIG_DIRS="${XDG_CONFIG_DIRS:-/etc/xdg}"
        export XDG_CONFIG_HOME="${XDG_CONFIG_HOME:-${HOME}/.config}"
        export XDG_DATA_DIRS="${XDG_DATA_DIRS:-/usr/local/share:/usr/share}"
        export XDG_DATA_HOME="${XDG_DATA_HOME:-${HOME}/.local/share}"
        export XDG_CACHE_HOME="${XDG_CACHE_HOME:-${HOME}/.cache}"
        export XDG_RUNTIME_DIR="${XDG_RUNTIME_DIR:-/run/user/$(id -u)}"

        export XDG_CURRENT_DESKTOP="${__WM_DESKTOP_NAMES__}"
        export XDG_SESSION_DESKTOP="${__WM_FIRST_DESKTOP_NAME__}"
        export XDG_MENU_PREFIX="${__WM_FIRST_DESKTOP_NAME__}-"

        #### apply quirks
        if type "quirks_${__WM_BIN_ID__}" >/dev/null
        then
                echo "Applying quirks for ${__WM_BIN_ID__}"
                "quirks_${__WM_BIN_ID__}" || return $?
        fi

        load_common_env || return $?

        if type "load_wm_env_${__WM_BIN_ID__}" >/dev/null
        then
                echo "Loading ENV for ${__WM_BIN_ID__}"
                "load_wm_env_${__WM_BIN_ID__}" || return $?
        else
                load_wm_env || return $?
                true
        fi
    """)

    # pass env after the mark
    shell_print_env = dedent(f"""
        printf "%s" "{random_mark}"
        env -0
    """)

    shell_full = '\n'.join([
        shell_definitions,
        shell_plugins_load,
        shell_main_body,
        shell_print_env
    ])

    print_debug(shell_full)

    return shell_full

def prepare_env():
    """
    Runs shell code to source native shell env fragments,
    Captures difference in env before and after,
    Filters it and exports to systemd user manager,
    Saves list for later cleanup.
    """

    # get current ENV
    env_pre = dict(os.environ)

    # Run shell code to prepare env and print results
    random_mark = f"MARK_{random_hex(16)}_MARK"
    shell_code = prepare_env_gen_sh(random_mark)

    stdout, stderr, returncode = wrap_process(
        ['sh', '-'],
        stdin=shell_code
    )

    # cut everything before and including random mark, also the last \0
    # treat stdout before the mark as messages
    mark_position = stdout.find(random_mark)
    if mark_position < 0:
        # print whole stdout
        if stdout.strip(): print_normal(stdout.strip())
        # print any stderr as errors
        if stderr.strip(): print_error(stderr.strip())
        raise Exception("No env mark in shell output!")
    else:
        stdout_msg = stdout[0:mark_position]
        stdout = stdout[mark_position + len(random_mark):].rstrip('\0')

    # print stdout if any
    if stdout_msg.strip(): print_normal(stdout_msg.strip())
    # print any stderr as errors
    if stderr.strip(): print_error(stderr.strip())

    if returncode != 0:
        raise Exception(f"Shell returned {returncode}!")

    # parse env
    env_post = dict()
    for env in stdout.split('\0'):
        env = env.split('=', maxsplit=1)
        if len(env) == 2:
            env_post.update({env[0]: env[1]})
        else:
            print_error(f"No value: {env}")

    # get systemd user manager ENV names
    systemd_varnames = get_systemd_varnames()

    ## Dict of vars to put into systemd user manager
    # raw difference dict between env_post and env_pre
    set_env = dict(set(env_post.items()) - set(env_pre.items()))

    print_debug('env_pre', env_pre)
    print_debug('env_post', env_post)
    print_debug('set_env', set_env)

    # add "always_export" vars from env_post to set_env
    for var in sorted(varnames.always_export - varnames.never_export - varnames.always_unset):
        if var in env_post:
            print_debug(f"Forcing export of {var}=\"{env_post[var]}\"")
            set_env.update({var: env_post[var]})

    # remove "never_export" and "always_unset" vars from set_env
    for var in varnames.never_export | varnames.always_unset:
        if var in set_env:
            print_debug(f"Excluding export of {var}")
            set_env.pop(var)

    # Set of vars to remove from systemd user manager
    # raw reverse difference
    unset_varnames = set(env_pre.keys()) - set(env_post.keys())
    # add "always_unset" vars
    unset_varnames = unset_varnames | set(varnames.always_unset)
    # leave only those that are defined in systemd user manager
    unset_varnames = unset_varnames & systemd_varnames

    # Set of vars to remove from systemd user manager on shutdown
    cleanup_varnames = set(set_env.keys()) | varnames.always_cleanup - varnames.never_cleanup

    # write cleanup file
    # first get exitsing vars if cleanup file already exists
    cleanup_file = os.path.join(BaseDirectory.get_runtime_dir(strict=True), f"env_names_for_cleanup_{wm_id}")
    if os.path.isfile(cleanup_file):
        with open(cleanup_file, 'r') as open_cleanup_file:
            current_cleanup_varnames = {l.strip() for l in open_cleanup_file.readlines() if l.strip()}
    else:
        current_cleanup_varnames = set()
    # write cleanup file
    with open(cleanup_file, 'w') as open_cleanup_file:
        open_cleanup_file.write('\n'.join(sorted(current_cleanup_varnames | cleanup_varnames)))

    # print message about env export
    set_env_msg = 'Exporting variables to systemd user manager:\n  ' + '\n  '.join(sorted(set_env.keys()))
    print_normal(set_env_msg)
    # export env by running systemctl in set_env environment
    stdout, stderr, returncode = wrap_process(
        ['systemctl', '--user', 'import-environment'] + sorted(set_env.keys()),
        env = env_post
    )
    if stdout.strip(): print_normal(stdout.strip())
    if stderr.strip(): print_error(stderr.strip())
    if returncode != 0:
        raise Exception(f"\"systemctl --user import-environment\" returned {returncode}")

    # export env by running dbus-update-activation-environment in set_env environment
    stdout, stderr, returncode = wrap_process(
        ['dbus-update-activation-environment', '--systemd'] + sorted(set_env.keys()),
        env = env_post
    )
    if stdout.strip(): print_normal(stdout.strip())
    if stderr.strip(): print_error(stderr.strip())
    if returncode != 0:
        raise Exception(f"\"dbus-update-activation-environment --systemd\" returned {returncode}")

    if unset_varnames:
        # print message about env unset
        unset_varnames_msg = 'Unsetting variables from systemd user manager:\n  ' + '\n  '.join(sorted(unset_varnames))
        print_normal(unset_varnames_msg)

        # unset env by running systemctl
        stdout, stderr, returncode = wrap_process(
            ['systemctl', '--user', 'unset-environment'] + sorted(unset_varnames),
        )
        if stdout.strip(): print_normal(stdout.strip())
        if stderr.strip(): print_error(stderr.strip())
        if returncode != 0:
            raise Exception(f"\"systemctl --user unset-environment\" returned {returncode}")

    # print message about future env cleanup
    cleanup_varnames_msg = 'Variables that will be removed from systemd user manager on stop:\n  ' + '\n  '.join(sorted(cleanup_varnames))
    print_normal(cleanup_varnames_msg)

def cleanup_env():
    """
    takes var names from "${XDG_RUNTIME_DIR}/env_names_for_cleanup_*"
    union varnames.always_cleanup,
    difference varnames.never_cleanup,
    intersect actual systemd user manager varnames,
    and remove them from systemd user manager.
    Remove found cleanup files
    """
    cleanup_file_dir = BaseDirectory.get_runtime_dir(strict=True)
    cleanup_files = []
    for cleanup_file in os.listdir(cleanup_file_dir):
        if not cleanup_file.startswith('env_names_for_cleanup_'):
            continue
        cleanup_file = os.path.join(cleanup_file_dir, cleanup_file)
        if os.path.isfile(cleanup_file):
            print_normal(f"Found cleanup_file \"{os.path.basename(cleanup_file)}\"")
            cleanup_files.append(cleanup_file)

    if not cleanup_files:
        print_warning('No cleanup files found')
        sys.exit(0)

    current_cleanup_varnames = set()
    for cleanup_file in cleanup_files:
        if os.path.isfile(cleanup_file):
            with open(cleanup_file, 'r') as open_cleanup_file:
                current_cleanup_varnames = current_cleanup_varnames | {l.strip() for l in open_cleanup_file.readlines() if l.strip()}

    systemd_varnames = get_systemd_varnames()

    cleanup_varnames = current_cleanup_varnames | varnames.always_cleanup - varnames.never_cleanup & systemd_varnames

    if cleanup_varnames:
        cleanup_varnames_msg = 'Cleaning up variables from systemd user manager:\n  ' + '\n  '.join(sorted(cleanup_varnames))
        print_normal(cleanup_varnames_msg)

        # nullify vars in dbus environment
        stdout, stderr, returncode = wrap_process(
            ['dbus-update-activation-environment', '--systemd'] + sorted([f"{n}=" for n in cleanup_varnames]),
        )
        if stdout.strip(): print_normal(stdout.strip())
        if stderr.strip(): print_error(stderr.strip())
        if returncode != 0:
            print_error(f"\"dbus-update-activation-environment --systemd\" returned {returncode}")

        stdout, stderr, returncode = wrap_process(
            ['systemctl', '--user', 'unset-environment'] + sorted(cleanup_varnames),
        )
        if stdout.strip(): print_normal(stdout.strip())
        if stderr.strip(): print_error(stderr.strip())
        if returncode != 0:
            raise Exception(f"\"systemctl --user unset-environment\" returned {returncode}")

    for cleanup_file in cleanup_files:
        os.remove(cleanup_file)
        print_ok(f"Removed \"{os.path.basename(cleanup_file)}\"")

def fill_wm_globals():
    'Fills global vars wm_argv, wm_id, wm_bin_id, wm_desktop_names, wm_name, wm_description based on args or desktop entry'

    global wm_argv
    global wm_cli_argv
    global wm_id
    global wm_bin_id
    global wm_desktop_names
    global wm_cli_desktop_names
    global wm_cli_desktop_names_exclusive
    global wm_name
    global wm_cli_name
    global wm_description
    global wm_cli_description

    wm_id = args.wm

    if not wm_id:
        print_error("WM is not provided")
        parsers['start'].print_help(file=sys.stderr)
        sys.exit(1)

    elif not re.search('^[a-zA-Z0-9_.-]+$', wm_id):
        print_error(f"\"{wm_id}\" does not conform to \"^[a-zA-Z0-9_.-]+$\" pattern")
        sys.exit(1)

    elif wm_id.endswith('.desktop'):
        print_debug(f"WM ID is a desktop entry: {wm_id}")
        # if WM ID is a desktop entry id
        try:
            # find and parse entry
            entry = get_de(wm_id)
            if not entry:
                raise Exception(f"Could not find and parse entry \"{wm_id}\"")
        except Exception as E:
            print_error(E)
            sys.exit(1)

        print_debug(entry)

        # combine exec from entry and arguments
        # TODO: either drop this behavior, or add support for % fields
        # not that wayland session entries will ever use them
        wm_argv = shlex.split(entry.getExec()) + args.args

        # this does not happen in aux exec mode
        if 'desktop_names' in args:
            # prepend desktop names from entry
            if args.desktop_names_exclusive:
                wm_desktop_names = sane_split(args.desktop_names, ':')
            else:
                if entry.get('DesktopNames', ''):
                    wm_desktop_names = [wm_argv[0]] + sane_split(entry.get('DesktopNames', ''), ':') + sane_split(args.desktop_names, ':')
                else:
                    wm_desktop_names = [wm_argv[0]] + sane_split(args.desktop_names, ':')

            if args.wm_name:
                wm_name = args.wm_name
            else:
                wm_name = ' '.join([
                    entry.getName() or os.path.splitext(os.path.basename(entry.filename))[0],
                    entry.get('GenericName', 'Window Manager')
                ])

            if args.wm_comment:
                wm_description = args.wm_comment
            else:
                wm_description = entry.get('Comment', '')

    else:
        # WM id is an executable
        # check exec
        if not which(wm_id):
            print_error(f"\"{wm_id}\" is not in PATH.")
            sys.exit(1)

        # combine argv
        wm_argv = [wm_id] + args.args

        # this does not happen in aux exec mode
        if 'desktop_names' in args:
            # fill other data
            if args.desktop_names_exclusive:
                wm_desktop_names = sane_split(args.desktop_names, ':')
            else:
                wm_desktop_names = [wm_argv[0]] + sane_split(args.desktop_names, ':')
            wm_name = args.wm_name
            wm_description = args.wm_comment

    # fill cli-exclusive vars for reproduction in unit drop-ins
    wm_cli_argv = args.args
    # this does not happen in aux exec mode
    if 'desktop_names' in args:
        wm_cli_desktop_names = args.desktop_names
        wm_cli_desktop_names_exclusive = args.desktop_names_exclusive
        wm_cli_name = args.wm_name
        wm_cli_description = args.wm_comment

        # deduplicate desktop names
        wm_desktop_names = list(set(wm_desktop_names))

    # id for functions and env loading
    wm_bin_id = re.sub('(^[^a-zA-Z]|[^a-zA-Z0-9_])+', '_', wm_argv[0])

    return(True)

def stop_wm():
    "Stops WM if active, returns int returncode"
    if is_active():
        print_normal('Stopping WM')
        if not args.dry_run:
            stdout, stderr, returncode = wrap_process([
                'systemctl',
                '--user',
                'stop',
                'wayland-wm@*.service'
            ])
            if stdout.strip(): print_normal(stdout.strip())
            if stderr.strip(): print_error(stderr.strip())
        else:
            returncode = 0
    else:
        print_normal('WM is not running')
        returncode = 0

    return returncode

def stop_wm_and_exit():
    "for use in signal trap"
    returncode = stop_wm()
    if returncode != 0: print_error(f"Stop returned {returncode}")
    sys.exit(returncode)

if __name__ == '__main__':

    # define globals
    units_changed = False
    wm_argv = []
    wm_cli_argv = []
    wm_id = ''
    wm_bin_id = ''
    wm_desktop_names = []
    wm_cli_desktop_names = []
    wm_cli_desktop_names_exclusive = False
    wm_name = ''
    wm_cli_name = ''
    wm_description = ''
    wm_cli_description = ''

    # get args and parsers (for help)
    args, parsers = parse_args()

    print_debug('args', args)

    #### START
    if args.mode == 'start':
        # Get ID from whiptail menu
        if args.wm in ['select', 'default']:
            try:
                select_wm_id = select_de()
                if select_wm_id:
                    try:
                        save_default_de(select_wm_id)
                    except Exception as E:
                        print_error(E)
                        sys.exit(1)
                    # update args.wm in place
                    args.wm = select_wm_id
                else:
                    print_error("No WM was selected")
                    sys.exit(1)
            except Exception as E:
                print_error(E)
                sys.exit(1)

        fill_wm_globals()

        print_normal(dedent(f"""
            Selected WM ID: {wm_id}
              Command Line: {shlex.join(wm_argv)}
          Plugin/binary ID: {wm_bin_id}
             Desktop Names: {':'.join(wm_desktop_names)}
                      Name: {wm_name}
               Description: {wm_description}
        """))

        if is_active(verbose=True) and not args.dry_run:
            print_error("Another WM is already running.")
            sys.exit(1)

        generate_units()

        if units_changed:
            reload_systemd()
        else:
            print_normal('Units unchanged')

        if args.only_generate:
            print_warning('Only unit creation was requested. Will not go further.')
            sys.exit(0)

        print_normal(f"Starting {wm_id}...")

        stdout, stderr, returncode = wrap_process([
            'systemctl', 'is-active', '-q', 'graphical.target'
        ])
        if returncode != 0:
            print_warning(dedent("""
                System has not reached graphical.target. It might be a good idea to screen for this with a condition.
                Will continue in 3 seconds...
            """))
            time.speep(3)

        if args.dry_run:
            print_warning('Dry Run Mode. Will not go further.')
            sys.exit(0)

        # trap exit on INT TERM HUP EXIT
        signal.signal(signal.SIGINT, stop_wm_and_exit)
        signal.signal(signal.SIGTERM, stop_wm_and_exit)
        signal.signal(signal.SIGHUP, stop_wm_and_exit)

        stdout, stderr, returncode = wrap_process(
            ['systemctl', '--user', 'start', '--wait', f"wayland-wm@{wm_id}.service"],
            stdout=sys.stdout,
            stderr=sys.stderr
        )

        print_normal(f"WM service stopped, systemctl returned {returncode}")

    #### STOP
    elif args.mode == 'stop':

        returncode = stop_wm()
        if returncode != 0: print_error(f"Stop returned {returncode}")

        # args.remove_units is False when not given, None if given without argument
        if args.remove_units != False:
            remove_units(args.remove_units)
            if units_changed:
                reload_systemd()
            else:
                print_normal('Units unchanged')

        sys.exit(returncode)

    #### FINALIZE
    elif args.mode == 'finalize':
        finalize(args.env_names)

    #### APP
    elif args.mode == 'app':
        if args.slice == 'a': slice = 'app-graphical.slice'
        elif args.slice == 'b': slice = 'background-graphical.slice'
        elif args.slice == 's': slice = 'session-graphical.slice'
        elif args.slice.endswith('.slice'): slice = args.slice
        else:
            print_error(f"Invalid slice name: {args.slice}")
            sys.exit(1)

        # use XDG_SESSION_DESKTOP as part of scope name
        stdout, stderr, returncode = wrap_process(['systemd-escape', os.getenv('XDG_SESSION_DESKTOP', 'uwsm')])
        if returncode != 0 or not stdout.strip():
            print_error(stderr)
            print_error("Could not escape sequence for scope name")
            sys.exit(1)
        desktop_scopename = stdout.strip()

        # use app cmd as part of scope name
        stdout, stderr, returncode = wrap_process(['systemd-escape', os.path.basename(args.cmd)])
        if returncode != 0 or not stdout.strip():
            print_error(stderr)
            print_error("Could not escape sequence for scope name")
            sys.exit(1)
        cmd_scopename = stdout.strip()

        os.execlp(
            'systemd-run',
            'systemd-run',
            '--user',
            '--scope',
            '--slice',
            slice,
            '-u',
            f"app-{desktop_scopename}-{cmd_scopename}-{random_hex(8)}.scope",
            '-qG',
            args.cmd,
            *args.args
        )

    #### CHECK
    elif args.mode == 'check' and args.checker == 'is-active':
        if is_active(args.wm, args.verbose):
            sys.exit(0)
        else:
            sys.exit(1)

    elif args.mode == 'check' and args.checker == 'may-start':
        dealbreakers = list()
        if is_active(): dealbreakers.append("Another WM is running")

        # check if parent process is a login shell
        try:
            with open(f"/proc/{os.getppid()}/cmdline", 'r') as ppcmdline:
                parent_cmdline = ppcmdline.read()
                parent_cmdline = parent_cmdline.strip()
            print_debug(f'parent_pid: {os.getppid()}')
            print_debug(f'parent_cmdline: {parent_cmdline}')
        except exception as E:
            print_error("Could not determine parent process command")
            print_error(E)
            sys.exit(1)
        if not parent_cmdline.startswith('-'): dealbreakers.append("Not in login shell")

        # check foreground VT
        stdout, stderr, returncode = wrap_process(['fgconsole'])
        fgvt = stdout.strip()
        if not fgvt.isnumeric():
            dealbreakers.append("Could not determine foreground VT")
        else:
            fgvt = int(fgvt)
            # argparse does not pass default for this
            allowed_vtnr = args.vtnr or [1]
            if fgvt not in allowed_vtnr:
                dealbreakers.append(f"Foreground VT ({fgvt}) is not among allowed VTs ({'|'.join([str(v) for v in allowed_vtnr])})")

        # check for graphical target
        stdout, stderr, returncode = wrap_process([
            'systemctl', 'is-active', '-q', 'graphical.target'
        ])
        if stdout.strip(): print_normal(stdout.strip())
        if stderr.strip(): print_normal(stderr.strip())
        if returncode != 0:
            dealbreakers.append("System has not reached graphical.target")

        if dealbreakers:
            if args.verbose: print_warning('\n'.join(['May not start WM:'] + dealbreakers))
            sys.exit(1)
        else:
            if args.verbose: print_ok('May start WM')
            sys.exit(0)

    #### AUX
    elif args.mode == 'aux':
        manager_pid = int(os.getenv('MANAGERPID', ''))
        ppid = int(
        os.getppid())
        print_debug(f"manager_pid: {manager_pid}, ppid: {ppid}")
        if not manager_pid or manager_pid != ppid:
            print_error("Aux actions can only be run by systemd user manager")
            sys.exit(1)

        if args.aux_action == 'prepare-env':
            fill_wm_globals()
            #DEBUG
            is_active(wm_id, True)
            try:
                prepare_env()
                sys.exit(0)
            except Exception as E:
                print_error(E)
                cleanup_env()
                sys.exit(1)
        elif args.aux_action == 'cleanup-env':
            if is_active('', True):
                print_error("A WM is running, will not cleanup environment")
                sys.exit(1)
            else:
                try:
                    cleanup_env()
                    sys.exit(0)
                except Exception as E:
                    print_error(E)
                    sys.exit(1)
        elif args.aux_action == 'exec':
            fill_wm_globals()
            print_debug(wm_argv)
            os.execlp(wm_argv[0], *(wm_argv))