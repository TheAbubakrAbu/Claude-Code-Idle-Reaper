#!/bin/sh
# Hands a Bash hook event to idle-reaper.py only when the event could involve a simulator, so
# every other command pays for one `case` and nothing more. idle-reaper.py makes the real check.
# idle-reaper.py is the one installed next to this file.
input=$(cat)
case "$input" in
  *simctl*|*xcodebuild*|*Simulator*|*"expo "*|*"flutter "*|*react-native*|*maestro*|*fastlane*)
    printf '%s' "$input" | /usr/bin/python3 "$(dirname "$0")/idle-reaper.py" hook >/dev/null 2>&1 ;;
esac
exit 0
