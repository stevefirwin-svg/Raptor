@echo off
cd /d C:\Users\steve\OneDrive\Desktop\Raptor

echo [%date% %time%] Starting daily GitHub push... >> logs\github_push.log

git add .
git commit -m "Daily update %date:~10,4%-%date:~4,2%-%date:~7,2%" >> logs\github_push.log 2>&1

git push >> logs\github_push.log 2>&1

echo [%date% %time%] Push complete. >> logs\github_push.log
echo GitHub push complete.
