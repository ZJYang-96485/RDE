from workflow.protocol_loader import load_protocol, list_protocols

print("Available protocols:")
for item in list_protocols():
    print(f"- {item['protocol_name']} | {item['display_name']} | steps={item['step_count']}")

print("\nLoading compact protocol:")
protocol = load_protocol("bulk_electrode_ca_steps_with_backward_compact")

print("protocol_name:", protocol["protocol_name"])
print("display_name:", protocol["display_name"])
print("step_count:", len(protocol["steps"]))

print("\nFirst 10 steps:")
for i, step in enumerate(protocol["steps"][:10], start=1):
    print(i, step["technique"], step["name"], step.get("voltage_v"), step.get("output"))

print("\nLast 10 steps:")
start = max(1, len(protocol["steps"]) - 9)
for i, step in enumerate(protocol["steps"][-10:], start=start):
    print(i, step["technique"], step["name"], step.get("voltage_v"), step.get("output"))