package backend

import (
	"encoding/json"
	"strings"
	"testing"
)

// A fresh session's snapshot has nothing in it; its lists must still be arrays.
func TestEmptyTranscriptMarshalsArrays(t *testing.T) {
	body, err := json.Marshal(Transcript{Status: StatusIdle})
	if err != nil {
		t.Fatal(err)
	}
	for _, want := range []string{`"messages":[]`, `"permissions":[]`, `"todos":[]`} {
		if !strings.Contains(string(body), want) {
			t.Errorf("missing %s in %s", want, body)
		}
	}
}
