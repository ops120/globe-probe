$r = Invoke-RestMethod -Uri 'http://127.0.0.1:8620/api/nodes'
Write-Host ('count=' + $r.value.Count)
foreach($n in $r.value){ Write-Host ($n.name + ' status=' + $n.status + ' hb_tasks=' + $n.hb_tasks) }
