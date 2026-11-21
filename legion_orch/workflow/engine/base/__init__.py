"""The base every engine layer builds on: a change's state and records (:class:`.ChangeCore`,
:class:`.StateStore`), the shared resources (:class:`.Services`, :class:`.EngineHost`, sandbox
identity), node ids, and the dispatcher with the work it runs (:mod:`.dispatch`, :mod:`.work`).
It imports no other engine layer."""
