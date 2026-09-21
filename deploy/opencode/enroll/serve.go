package main

import "fmt"

const serveUsage = `enroll serve — run the local refreshing proxy shim.

Usage:
  enroll serve [--creds PATH] [--port PORT]

  --creds  Credential file written by 'enroll enroll' (default
           <config-dir>/opencode/pystino-credentials.json).
  --port   Override the loopback port recorded at enroll time.

opencode points its baseURL at http://127.0.0.1:<port>/v1; the shim
injects a fresh access token and the recorded x-bill-to on every request.
`

// runServe lands with the shim (phase 3). Until then it says so plainly
// rather than half-proxying without refresh, which would fail opaquely
// inside opencode on the first token expiry.
func runServe(args []string) error {
	fs := flagSetWithHelp("serve", serveUsage)
	_ = fs
	if err := fs.Parse(args); err != nil {
		return err
	}
	return fmt.Errorf("serve is not implemented yet")
}
