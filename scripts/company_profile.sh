#!/usr/bin/env bash
cd /d01/fundos/src && source /d01/fundos/app/venv/bin/activate
set -a; source /etc/fundos/fundos.env; set +a
python manage.py shell <<'PY'
from django.apps import apps
Co = next(m for m in apps.get_models() if m.__name__ == 'Company')
for c in Co.objects.order_by('-created_at')[:10]:
    print(getattr(c,'id',None), getattr(c,'name', getattr(c,'legal_name','')))
PY

