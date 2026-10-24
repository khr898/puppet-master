from flint_bridge import lifecycle, sysinfo

VM_STAT = """Mach Virtual Memory Statistics: (page size of 16384 bytes)
Pages free:                                     6565.
Pages active:                                 248968.
Pages inactive:                               642989.
Pages speculative:                              2943.
Pages throttled:                                   0.
Pages wired down:                             156919.
Pages purgeable:                                9393.
File-backed pages:                            696123.
Anonymous pages:                              198777.
"""


def test_available_counts_free_file_cache_and_purgeable_pages():
    gb = sysinfo.parse_vm_stat(VM_STAT)
    assert gb == (6565 + 696123 + 9393) * 16384 / 1e9
    assert 11.5 < gb < 11.8


def test_inactive_anonymous_pages_are_not_counted_as_available():
    bigger = VM_STAT.replace("642989.", "99999999.")
    assert sysinfo.parse_vm_stat(bigger) == sysinfo.parse_vm_stat(VM_STAT)


def test_unparseable_output_gives_none():
    assert sysinfo.parse_vm_stat("") is None
    assert sysinfo.parse_vm_stat("Pages free: 5.") is None  # no page size, no file-backed


def test_available_gb_is_none_when_vm_stat_fails(monkeypatch):
    def boom(*a, **kw):
        raise OSError("no vm_stat")

    monkeypatch.setattr(sysinfo.subprocess, "run", boom)
    assert sysinfo.available_gb() is None


def test_lifecycle_free_gb_uses_available_memory(monkeypatch):
    monkeypatch.setattr(sysinfo, "available_gb", lambda: 7.5)
    assert lifecycle.free_gb() == 7.5
