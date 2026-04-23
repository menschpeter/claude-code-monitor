# 1. Abhängigkeiten
pip install rich
sudo apt install jq     # oder brew install jq

# 2. Dateien in denselben Ordner legen, dann Hook installieren
python cc-session-monitor.py --install-hook
# → kopiert cc-monitor-hook.sh nach ~/.claude/cc-monitor-hook.sh
# → erweitert ~/.claude/settings.json um den statusLine-Eintrag

# 3. Claude Code neu starten, dann Monitor in einem zweiten Terminal starten
python cc-session-monitor.py
