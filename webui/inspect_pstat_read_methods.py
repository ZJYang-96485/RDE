import inspect
import toolkitpy as tkp

tkp.toolkitpy_init("inspect_pstat_read_methods.py")

pstat_name = tkp.enum_sections()[0]
pstat = tkp.Pstat(pstat_name)

print("PSTAT:", pstat_name)

keywords = [
    "measure",
    "meas",
    "read",
    "volt",
    "current",
    "curr",
    "cell",
    "ocv",
    "open"
]

for name in dir(pstat):
    lower = name.lower()
    if any(k in lower for k in keywords):
        method = getattr(pstat, name)
        print("\n---", name, "---")
        print(getattr(method, "__doc__", None))
        try:
            print("signature:", inspect.signature(method))
        except Exception as exc:
            print("signature unavailable:", exc)

del pstat
