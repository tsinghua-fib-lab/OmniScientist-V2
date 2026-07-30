"""Where the walk for project skills has to stop, and why that is a boundary.

A skill is executable: discovering one means a later turn may run its engine or
follow its instructions. Project skills are discovered by walking *up* from the
working directory, which makes every ancestor of that directory a potential
source of code. The walk therefore stops at the user's home — anything at or
above it is a *user* root, adopted deliberately through the ``user_*`` sources,
never picked up because a session happened to start somewhere underneath.

Without that stop, a ``.claude/skills`` directory planted in a shared parent —
``/Users`` on a multi-account machine, a mounted team share, an unpacked archive
above the checkout — is injected into every session started below it, and it
arrives labelled as the project's own. Nothing else in the suite held this: the
guard survived being deleted.
"""

from __future__ import annotations

from pathlib import Path

from omni.config.paths import iter_project_skill_dirs


def _plant(root: Path) -> Path:
    """Create ``<root>/.claude/skills`` and hand back the directory."""
    skills = root / ".claude" / "skills"
    skills.mkdir(parents=True, exist_ok=True)
    return skills


def test_a_skill_directory_in_the_home_is_not_the_project_s_own() -> None:
    """``~/.claude/skills`` is a user root; the project walk must not claim it.

    It is discovered by omni either way — the distinction is *as what*. Claimed
    as a project root it inherits the project's trust and the project's
    precedence, so a user-level skill would silently shadow the checkout's own.
    """
    home = Path.home().resolve()
    _plant(home)
    checkout = home / "work" / "repo"
    own = _plant(checkout)

    assert iter_project_skill_dirs(checkout) == [own]


def test_a_skill_directory_above_the_home_never_enters_a_session() -> None:
    """The shared-parent case, which is the one with an attacker in it.

    Whoever can write to a directory above the home can write to it for every
    account below, so a skill planted there would be executable by all of them.
    The home stop is what the walk hits first; this pins that it is reached
    before any ancestor is considered, not merely that ancestors are filtered.
    """
    home = Path.home().resolve()
    planted = _plant(home.parent)
    checkout = home / "work" / "repo"
    own = _plant(checkout)

    found = iter_project_skill_dirs(checkout)

    assert planted not in found
    assert found == [own]


def test_the_checkout_s_own_ancestors_are_still_project_roots() -> None:
    """The stop is a boundary, not a refusal to walk.

    A monorepo keeps shared skills at its root and per-package skills beside the
    package; both are the project's. A guard that broke this would be safe and
    useless, which is the failure mode worth naming next to the one above.
    """
    home = Path.home().resolve()
    monorepo = home / "work" / "monorepo"
    shared = _plant(monorepo)
    (monorepo / ".git").mkdir(parents=True, exist_ok=True)
    package = monorepo / "packages" / "engine"
    own = _plant(package)

    assert iter_project_skill_dirs(package) == [own, shared]
