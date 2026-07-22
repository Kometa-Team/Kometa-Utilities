package main

import (
	"fmt"
	"os"
	"strings"
	"syscall"
)

const (
	secretPath  = "/run/secrets/virustotal_key"
	transferBin = "/go/bin/transfersh"
)

func main() {
	secret, err := os.ReadFile(secretPath)
	if err != nil {
		fmt.Fprintf(os.Stderr, "read VirusTotal secret: %v\n", err)
		os.Exit(1)
	}

	key := strings.TrimSpace(string(secret))
	if key == "" {
		fmt.Fprintln(os.Stderr, "VirusTotal secret is empty")
		os.Exit(1)
	}
	if err := os.Setenv("VIRUSTOTAL_KEY", key); err != nil {
		fmt.Fprintf(os.Stderr, "set VirusTotal environment: %v\n", err)
		os.Exit(1)
	}

	args := append([]string{transferBin}, os.Args[1:]...)
	if err := syscall.Exec(transferBin, args, os.Environ()); err != nil {
		fmt.Fprintf(os.Stderr, "start transfer.sh: %v\n", err)
		os.Exit(1)
	}
}
