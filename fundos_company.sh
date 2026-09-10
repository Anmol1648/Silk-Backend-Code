# Extract all recent profile generation logs
grep -A 500 "profile.generate\[" /d01/fundos/logs/celery-worker.log | head -1000 > fundos_profile_log.txt

# Get database info
cd /d01/fundos/src
source /d01/fundos/app/venv/bin/activate
set -a; source /etc/fundos/fundos.env; set +a

python manage.py shell << 'PYEOF' > fundos_metadata.txt
from fundos.profile.models import ProfileGenerationRun
from fundos.llm.models import LLMCallLog
import json

# Get latest run
run = ProfileGenerationRun.objects.order_by('-created_at').first()

if run:
    print(f"Company: {run.company.name}")
    print(f"Run ID: {run.run_id}")
    print(f"Status: {run.status}")
    print(f"Duration (ms): {run.duration_ms}")
    print(f"Started: {run.started_at}")
    print(f"Completed: {run.completed_at}")
    print(f"LLM Calls: {run.llm_calls}")
    print(f"Sections: {run.sections_count}")
    print(f"Fields: {run.fields_count}")
    print(f"Completeness: {run.completeness_pct}%")
    print(f"Cost (₹): {run.total_cost_inr}")
    print()
    
    # Get related LLM calls
    calls = LLMCallLog.objects.filter(run_id=run.run_id)
    print(f"Total LLM Calls: {calls.count()}")
    print()
    for call in calls:
        print(f"  Role: {call.role}")
        print(f"    Status: {call.status}")
        print(f"    Latency: {call.latency_ms}ms")
        print(f"    Tokens: in={call.prompt_tokens}, out={call.completion_tokens}")
        print()
else:
    print("No runs found")
PYEOF

cat fundos_profile_log.txt fundos_metadata.txt > fundos_complete.log

