"""Sandbox names. The OpenShell gateway accepts a name only if it is a DNS label of at most 19
characters without ``--`` (MAX_ROUTABLE_NAME_LEN; NVIDIA/OpenShell#2651), and names are unique
per gateway, so they must not collide across changes or controller state directories."""
from __future__ import annotations

import re
import secrets
from collections.abc import Callable

MAX_NAME = 19
_LABEL = re.compile(r"^[a-z0-9]([a-z0-9-]*[a-z0-9])?$")


def check_name(name: str) -> str:
    """Refuse a name the gateway would refuse – here, before anything is created."""
    if len(name) > MAX_NAME or not _LABEL.match(name) or "--" in name:
        raise ValueError(f"sandbox name {name!r} is not a DNS label of at most {MAX_NAME} "
                           "characters without '--' (OpenShell gateway limit)")
    return name


# Readable names: <prefix>-<first name>, e.g. lg-ada (agents) and lgv-omar (verifiers). Names
# are never reused; once every name has been used, a number is added (lg-ada-2). Names are at
# most 8 letters so "lgv-" + name + a 4-digit suffix still fits 19 characters.
NAMES = """
aaron abby ada adele aditi adrian agnes ahmed aiden aisha akira alan albert alex alice alma
amara amelia amir amy ana andre angela anika anna anton arjun arthur asha astrid aurora ava
axel ayumi barbara basil beatriz bella ben bianca boris bruno caleb camila carla carlos carmen
caspar cecil celia chen chiara chloe clara colin cora cyrus daisy dalia daniel dante david
delia diana diego dmitri dora eden edgar edith edwin elena eli eliza ella elsa emil emma enzo
erik esme esther ethan eva evan ezra faisal farah felix fiona flora frank freya gabriel gemma
george gia gloria grace greta gus hana hannah harper harriet hassan hazel hector helen henry
hiro hugo ian ida ilse imani ines ingrid iris isaac isla ivan ivy jack jade james jana jasper
javier jay jin joan joel jonas jorge josie juan jude julia june kai kamala karim kate kenji
kira kofi lara laszlo laura layla lea leila lena leo leon lila lily lina linus liam lola
lucas lucia luis luna maeve malik mara marco maria marta mateo maya mei mila milo mina
miriam moira nadia naomi nathan nell nia nico nina noah nora oliver olga omar oscar otto
paige paolo pavel pearl petra pia priya quinn rafael rania ravi rhea rita rosa ruby rufus
ryan sacha sadie sam sana sara selma sergei silas simon sofia stella sven talia tara theo
thea tomas uma una valeria vera victor viola wanda wendy willa xavier yara yasmin yuki
yusuf zara zeke zoe zora
""".split()
AGENT_PREFIX, VERIFIER_PREFIX = "lg", "lgv"


def human_name(prefix: str, taken: Callable[[str], bool]) -> str:
    """A random first name not yet ``taken``: ``<prefix>-<name>``, then ``<prefix>-<name>-2``,
    ``-3``, … once every plain name is used."""
    for n in range(1, 10_000):
        suffix = f"-{n}" if n > 1 else ""
        free = [f"{prefix}-{name}{suffix}" for name in NAMES]
        free = [c for c in free if not taken(c) and len(c) <= MAX_NAME]
        if free:
            return check_name(secrets.choice(free))
    return check_name(f"{prefix}-{secrets.token_hex((MAX_NAME - len(prefix) - 1) // 2)}")


def is_name_clash(error: Exception) -> bool:
    """The gateway refused a name because a sandbox with it already exists (e.g. created by
    another controller sharing the gateway)."""
    return "already exists" in str(error).lower()
