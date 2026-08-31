package server

import (
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"testing"

	v1beta1 "github.com/agentscope-ai/AgentTeams/agentteams-controller/api/v1beta1"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"sigs.k8s.io/controller-runtime/pkg/client/fake"
)

func writeSkill(t *testing.T, dir, name, description string) {
	t.Helper()
	if err := os.MkdirAll(filepath.Join(dir, name), 0o755); err != nil {
		t.Fatal(err)
	}
	content := "---\nname: " + name + "\ndescription: " + description + "\n---\n\n# " + name + "\n"
	if err := os.WriteFile(filepath.Join(dir, name, "SKILL.md"), []byte(content), 0o644); err != nil {
		t.Fatal(err)
	}
}

// newSkillsRig builds a template tree:
//
//	base/worker-agent/skills/{file-sync,find-skills}
//	base/copaw-worker-agent/skills/{file-sync,task-progress}
//	base/team-leader-agent/skills/{leader-briefing}
//	(no hermes dir; a stray non-dir file in worker-agent/skills; a SKILL.md
//	without frontmatter)
func newSkillsRig(t *testing.T) (*SkillsHandler, string) {
	t.Helper()
	base := t.TempDir()
	writeSkill(t, filepath.Join(base, "worker-agent", "skills"), "file-sync", "Sync files with centralized storage.")
	writeSkill(t, filepath.Join(base, "worker-agent", "skills"), "find-skills", "Discover skills from the open ecosystem.")
	if err := os.WriteFile(filepath.Join(base, "worker-agent", "skills", "notes.txt"), []byte("x"), 0o644); err != nil {
		t.Fatal(err)
	}
	writeSkill(t, filepath.Join(base, "copaw-worker-agent", "skills"), "file-sync", "Sync files with centralized storage (copaw).")
	writeSkill(t, filepath.Join(base, "copaw-worker-agent", "skills"), "task-progress", "Report task progress.")
	writeSkill(t, filepath.Join(base, "team-leader-agent", "skills"), "leader-briefing", "Brief team members.")

	scheme := newServerTestScheme(t)
	worker := &v1beta1.Worker{
		ObjectMeta: metav1.ObjectMeta{Name: "alpha-dev", Namespace: "default"},
		Spec: v1beta1.WorkerSpec{RemoteSkills: []v1beta1.RemoteSkillSource{
			{Source: "nacos", Skills: []v1beta1.RemoteSkill{{Name: "web-research"}}},
			{Source: "nacos", Skills: []v1beta1.RemoteSkill{{Name: "file-sync"}}}, // also builtin
		}},
	}
	k8sClient := fake.NewClientBuilder().WithScheme(scheme).WithObjects(worker).Build()
	dir := filepath.Join(base, "worker-agent")
	return NewSkillsHandler(k8sClient, "default", dir), base
}

func getSkills(t *testing.T, h *SkillsHandler) *httptest.ResponseRecorder {
	t.Helper()
	rec := httptest.NewRecorder()
	h.ListSkills(rec, httptest.NewRequest(http.MethodGet, "/api/v1/skills", nil))
	return rec
}

func TestListSkills_BuiltinAndRemoteCatalog(t *testing.T) {
	handler, _ := newSkillsRig(t)
	rec := getSkills(t, handler)
	if rec.Code != http.StatusOK {
		t.Fatalf("expected 200, got %d: %s", rec.Code, rec.Body.String())
	}

	var resp SkillListResponse
	if err := json.Unmarshal(rec.Body.Bytes(), &resp); err != nil {
		t.Fatalf("unmarshal: %v", err)
	}
	got := map[string]SkillInfo{}
	for _, s := range resp.Skills {
		got[s.Name] = s
	}
	if resp.Total != len(got) {
		t.Fatalf("total %d != len(skills) %d", resp.Total, len(got))
	}

	// Builtin skill present with description and providing agents.
	fs, ok := got["file-sync"]
	if !ok {
		t.Fatalf("file-sync missing: %v", resp.Skills)
	}
	if fs.Source != "builtin+nacos" {
		t.Errorf("file-sync source = %q, want builtin+nacos", fs.Source)
	}
	if fs.Description == "" {
		t.Errorf("file-sync description empty")
	}
	if len(fs.Agents) != 2 || fs.Agents[0] != "copaw-worker-agent" || fs.Agents[1] != "worker-agent" {
		t.Errorf("file-sync agents = %v, want [copaw-worker-agent worker-agent]", fs.Agents)
	}

	// Pure-builtin skill.
	if lb, ok := got["leader-briefing"]; !ok || lb.Source != "builtin" {
		t.Errorf("leader-briefing = %+v, want builtin", lb)
	}
	// Pure remote skill.
	if wr, ok := got["web-research"]; !ok || wr.Source != "nacos" {
		t.Errorf("web-research = %+v, want nacos", wr)
	}
	// Non-dir file and missing skills dirs must not appear.
	if _, ok := got["notes.txt"]; ok {
		t.Errorf("stray file notes.txt leaked into catalog")
	}
	// Sorted output.
	for i := 1; i < len(resp.Skills); i++ {
		if resp.Skills[i-1].Name >= resp.Skills[i].Name {
			t.Fatalf("skills not sorted at %d: %v", i, resp.Skills)
		}
	}
}

func TestListSkills_MissingTemplateDir(t *testing.T) {
	scheme := newServerTestScheme(t)
	k8sClient := fake.NewClientBuilder().WithScheme(scheme).Build()
	handler := NewSkillsHandler(k8sClient, "default", filepath.Join(t.TempDir(), "does-not-exist", "worker-agent"))

	rec := getSkills(t, handler)
	if rec.Code != http.StatusOK {
		t.Fatalf("expected 200 with empty catalog, got %d: %s", rec.Code, rec.Body.String())
	}
	var resp SkillListResponse
	if err := json.Unmarshal(rec.Body.Bytes(), &resp); err != nil {
		t.Fatalf("unmarshal: %v", err)
	}
	if resp.Total != 0 {
		t.Errorf("expected empty catalog, got %d", resp.Total)
	}
}

func TestListSkills_EmptyWorkerAgentDir(t *testing.T) {
	handler := NewSkillsHandler(nil, "default", "")
	rec := getSkills(t, handler)
	if rec.Code != http.StatusOK {
		t.Fatalf("expected 200, got %d: %s", rec.Code, rec.Body.String())
	}
}

func TestParseSkillFrontmatter(t *testing.T) {
	dir := t.TempDir()

	cases := []struct {
		name    string
		content string
		wantN   string
		wantD   string
	}{
		{"valid", "---\nname: a\ndescription: d\n---\nbody\n", "a", "d"},
		{"no-frontmatter", "# just a heading\n", "", ""},
		{"name-only", "---\nname: b\n---\n", "b", ""},
		{"empty", "", "", ""},
	}
	for _, tc := range cases {
		path := filepath.Join(dir, tc.name+".md")
		if err := os.WriteFile(path, []byte(tc.content), 0o644); err != nil {
			t.Fatal(err)
		}
		n, d := parseSkillFrontmatter(path)
		if n != tc.wantN || d != tc.wantD {
			t.Errorf("%s: got (%q,%q), want (%q,%q)", tc.name, n, d, tc.wantN, tc.wantD)
		}
	}
	if n, d := parseSkillFrontmatter(filepath.Join(dir, "missing.md")); n != "" || d != "" {
		t.Errorf("missing file: got (%q,%q), want empty", n, d)
	}
}
