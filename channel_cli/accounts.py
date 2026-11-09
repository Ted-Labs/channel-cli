"""Claude profiles share policy files, never authentication or session data."""
import json
import os
import tempfile
from pathlib import Path

PROFILES = ('personal', 'team')
SHARED = ('settings.json', 'settings.local.json', 'CLAUDE.md', 'skills', 'hooks')


def selection_file():
    return Path.home() / '.config/atrium/claude-account.json'


def selected():
    path = selection_file()
    if not path.exists():
        return 'personal'
    value = json.loads(path.read_text()).get('account')
    if value not in PROFILES:
        raise ValueError('invalid Claude account selection')
    return value


def describe(profile=None):
    profile = selected() if profile is None else profile
    if profile not in PROFILES:
        raise ValueError('Claude account must be personal or team')
    directory = Path.home() / ('.claude-team' if profile == 'team' else '.claude')
    return {'account': profile, 'label': 'Team' if profile == 'team' else '개인',
            'config_dir': str(directory),
            'metadata_file': str(directory / '.claude.json' if profile == 'team' else Path.home() / '.claude.json'),
            'credentials_file': str(directory / '.credentials.json'), 'requires_restart': True}


def prepare_team():
    personal, team = Path.home() / '.claude', Path.home() / '.claude-team'
    # Check every destination before changing anything; preserve existing data.
    if team.is_symlink():
        raise ValueError('Team configuration directory must not be a symlink')
    for name in ('.credentials.json', '.claude.json'):
        secret = team / name
        personal_secret = personal / name if name == '.credentials.json' else Path.home() / '.claude.json'
        if secret.is_symlink() or secret.exists() and personal_secret.exists() and secret.samefile(personal_secret):
            raise ValueError(f'Team authentication must be separate: {name}')
    for name in SHARED:
        dest, source = team / name, personal / name
        if os.path.lexists(dest) and (not dest.is_symlink() or dest.resolve() != source.resolve()):
            raise ValueError(f'Team shared path already exists: {name}; preserve it before linking')
    team.mkdir(parents=True, exist_ok=True, mode=0o700)
    for name in SHARED:
        dest = team / name
        if not os.path.lexists(dest):
            dest.symlink_to(personal / name)


def choose(profile):
    result = describe(profile)
    if profile == 'team':
        prepare_team()
    path = selection_file()
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, temporary = tempfile.mkstemp(prefix='.claude-account-', dir=path.parent)
    try:
        with os.fdopen(fd, 'w') as file:
            json.dump({'version': 1, 'account': profile}, file)
            file.write('\n')
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    return result


def launch_env(environ=None):
    env = dict(os.environ if environ is None else environ)
    info = describe(env.get('ATRIUM_CLAUDE_ACCOUNT') or None)
    env['ATRIUM_CLAUDE_ACCOUNT'] = info['account']
    if info['account'] == 'team':
        env['CLAUDE_CONFIG_DIR'] = info['config_dir']
    else:
        # Setting this to ~/.claude changes the metadata location to
        # ~/.claude/.claude.json. Unset it to preserve ~/.claude.json.
        env.pop('CLAUDE_CONFIG_DIR', None)
    return env
