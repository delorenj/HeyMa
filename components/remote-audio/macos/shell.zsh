# Only interactive logins to Big Chungus use the audio connection.
# Remote commands, scp, other hosts and SSH options keep ordinary SSH behavior.
ssh() {
    if (( $# == 1 )) && [[ "$1" == big-chungus || "$1" == big-chungus.burro-salmon.ts.net ]]; then
        /usr/bin/python3 "$HOME/.local/share/ssh-audio/ssh_big_chungus.py" "$1"
    else
        command ssh "$@"
    fi
}
