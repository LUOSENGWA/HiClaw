// Package server — read-only coding-run transcript visibility (D12 路 A).
//
// See docs/design/coding-runs-api.md for the routes, the two-layer
// authorization (authorizer admits the action, the handler enforces the
// team scope + W4 404), and the status mapping.
//
// CodingRunsHandler exposes the one-shot coding CLI runs that
// run-coding-cli.sh (#1342) tees into coding-cli-logs/, so a human can see
// what a delegated coding task did — which files it touched, what the CLI
// output, why it timed out or failed — without logging into the worker to
// read a log file. It is the coding-CLI counterpart of the worker chats proxy
// (#1295): same read-only, byte-transparent, W8/W4 404-not-403 discipline.
//
// Where the data lives. The coding skill pins the run workspace under
// /root/agentteams-fs/shared/tasks/{task-id}/workspace and mc-mirrors it to
// object storage, so each run transcript sits at
//
//	shared/tasks/{taskID}/workspace/coding-cli-logs/run-{ts}.log   (standalone)
//	teams/{team}/shared/tasks/{taskID}/workspace/coding-cli-logs/run-{ts}.log (team)
//
// The controller does not mount the worker filesystem (helm mounts only
// /data/worker-creds), so it reads through oss.StorageClient (mc), exactly
// like the project task endpoints — which is also why this works in both
// embedded and K8s modes (it does not proxy a per-worker console port).
//
// Access. The caller's readable storage prefixes are team-scoped, mirroring
// the project endpoints: L1 reads team + global (standalone) tasks; an L2
// team-leader / human reads only their own team's prefix, and standalone
// tasks (the global shared/ prefix, which have no team ownership) stay
// L1-only. A task the caller cannot see 404s like a missing task (W4: no
// probing).
package server

import (
	"context"
	"net/http"
	"regexp"
	"strings"

	v1beta1 "github.com/agentscope-ai/AgentTeams/agentteams-controller/api/v1beta1"
	authpkg "github.com/agentscope-ai/AgentTeams/agentteams-controller/internal/auth"
	"github.com/agentscope-ai/AgentTeams/agentteams-controller/internal/httputil"
	"github.com/agentscope-ai/AgentTeams/agentteams-controller/internal/oss"
	"sigs.k8s.io/controller-runtime/pkg/client"
)

const (
	// codingRunsListCap bounds the number of runs returned by the list
	// endpoint (the project list cap discipline: a bounded response, not an
	// unbounded scan of every task).
	codingRunsListCap = 200
)

// codingRunFilePattern matches a run transcript filename: run-{YYYYMMDD}-
// {HHMMSS}.log, the shape run-coding-cli.sh writes.
var codingRunFilePattern = regexp.MustCompile(`^run-\d{8}-\d{6}\.log$`)

// CodingRunsHandler serves read-only coding-run transcripts from object
// storage.
type CodingRunsHandler struct {
	client    client.Client
	namespace string
	oss       oss.StorageClient
}

// NewCodingRunsHandler creates the handler. oss is the object-storage client
// (mc-backed in production, ossfake in tests).
func NewCodingRunsHandler(c client.Client, namespace string, o oss.StorageClient) *CodingRunsHandler {
	return &CodingRunsHandler{client: c, namespace: namespace, oss: o}
}

// taskLogPrefixes builds the dual-prefix task layout (team + global),
// mirroring ProjectHandler.teamProjectPrefixes but for coding runs (tasks,
// not projects). Returns (prefixes, crToEffective, error).
func (h *CodingRunsHandler) taskLogPrefixes(ctx context.Context) ([]string, map[string]string, error) {
	var teams v1beta1.TeamList
	if err := h.client.List(ctx, &teams, client.InNamespace(h.namespace)); err != nil {
		return nil, nil, err
	}
	prefixes := make([]string, 0, len(teams.Items)+1)
	crToEffective := make(map[string]string, len(teams.Items))
	seen := map[string]bool{}
	for i := range teams.Items {
		effective := teams.Items[i].Spec.EffectiveTeamName(teams.Items[i].Name)
		crToEffective[teams.Items[i].Name] = effective
		for _, name := range []string{effective, teams.Items[i].Name} {
			prefix := "teams/" + name + "/shared/tasks/"
			if !seen[prefix] {
				seen[prefix] = true
				prefixes = append(prefixes, prefix)
			}
		}
	}
	prefixes = append(prefixes, "shared/tasks/")
	return prefixes, crToEffective, nil
}

// visibleTaskPrefixes narrows the full prefix set to what a scoped caller may
// read. L1 (admin, and any caller that is neither a team leader nor an L2
// human) reads everything — team prefixes plus the global standalone prefix.
// An L2 caller reads only the prefix of each team they belong to; the global
// standalone prefix is deliberately excluded for them (standalone coding tasks
// have no team ownership and stay L1-only).
func visibleTaskPrefixes(all []string, caller *authpkg.CallerIdentity, crToEffective map[string]string) []string {
	if caller == nil || (caller.Role != authpkg.RoleTeamLeader && caller.Role != authpkg.RoleHuman) {
		return all
	}
	teams := caller.Teams
	if len(teams) == 0 && caller.Team != "" {
		teams = []string{caller.Team}
	}
	allowed := make(map[string]bool, len(teams))
	for _, t := range teams {
		eff := t
		if mapped, ok := crToEffective[t]; ok && mapped != "" {
			eff = mapped
		}
		allowed["teams/"+eff+"/shared/tasks/"] = true
	}
	out := make([]string, 0, len(allowed))
	for _, p := range all {
		if allowed[p] {
			out = append(out, p)
		}
	}
	return out
}

// codingRun is one run transcript entry in the list response.
type codingRun struct {
	TaskID string `json:"task_id"`
	Run    string `json:"run"`
	Team   string `json:"team,omitempty"`
}

// GET /api/v1/coding-runs
//
// Lists the coding-run transcripts the caller may read, across their visible
// storage prefixes, bounded by codingRunsListCap.
func (h *CodingRunsHandler) ListCodingRuns(w http.ResponseWriter, r *http.Request) {
	caller := authpkg.CallerFromContext(r.Context())
	prefixes, crToEffective, err := h.taskLogPrefixes(r.Context())
	if err != nil {
		writeK8sError(w, "list coding runs: resolve prefixes", err)
		return
	}
	runs := make([]codingRun, 0)
	for _, prefix := range visibleTaskPrefixes(prefixes, caller, crToEffective) {
		dirs, err := h.oss.ListObjects(r.Context(), prefix)
		if err != nil {
			continue // unreadable prefix (no such team's storage) — skip, don't fail the whole list
		}
		for _, d := range dirs {
			if !strings.HasSuffix(d, "/") {
				continue
			}
			taskID := strings.TrimSuffix(d, "/")
			files, err := h.oss.ListObjects(r.Context(), prefix+taskID+"/workspace/coding-cli-logs/")
			if err != nil {
				continue
			}
			for _, f := range files {
				if !codingRunFilePattern.MatchString(f) {
					continue
				}
				runs = append(runs, codingRun{TaskID: taskID, Run: f, Team: teamOfPrefix(prefix)})
				if len(runs) >= codingRunsListCap {
					break
				}
			}
			if len(runs) >= codingRunsListCap {
				break
			}
		}
		if len(runs) >= codingRunsListCap {
			break
		}
	}
	httputil.WriteJSON(w, http.StatusOK, runs)
}

// GET /api/v1/coding-runs/{taskID}/runs/{run}
//
// Serves one run transcript byte-transparent (the raw .log the CLI teed).
// The path segments are validated (isSafeTaskID + the run filename pattern)
// before any storage read, so they cannot carry path traversal. The run is
// looked up across the caller's visible prefixes; a miss 404s like a missing
// task (W4).
func (h *CodingRunsHandler) GetCodingRun(w http.ResponseWriter, r *http.Request) {
	taskID := r.PathValue("taskID")
	run := r.PathValue("run")
	if !isSafeTaskID(taskID) {
		httputil.WriteError(w, http.StatusBadRequest, "invalid task id")
		return
	}
	if !codingRunFilePattern.MatchString(run) {
		httputil.WriteError(w, http.StatusBadRequest, "invalid run id")
		return
	}
	caller := authpkg.CallerFromContext(r.Context())
	teamFilter := r.URL.Query().Get("team")
	prefixes, crToEffective, err := h.taskLogPrefixes(r.Context())
	if err != nil {
		writeK8sError(w, "get coding run: resolve prefixes", err)
		return
	}
	try := make([]string, 0, len(prefixes))
	for _, p := range visibleTaskPrefixes(prefixes, caller, crToEffective) {
		if teamFilter != "" && !strings.HasPrefix(p, "teams/"+teamFilter+"/") {
			continue
		}
		try = append(try, p)
	}
	for _, p := range try {
		key := p + taskID + "/workspace/coding-cli-logs/" + run
		data, err := h.oss.GetObject(r.Context(), key)
		if err != nil {
			continue // not under this prefix; try the next
		}
		w.Header().Set("Content-Type", "text/plain; charset=utf-8")
		w.WriteHeader(http.StatusOK)
		_, _ = w.Write(data)
		return
	}
	httputil.WriteError(w, http.StatusNotFound, "coding run not found")
}

// teamOfPrefix extracts the team name from a storage prefix for the list
// response ("" for the global standalone prefix).
func teamOfPrefix(prefix string) string {
	if strings.HasPrefix(prefix, "teams/") {
		rest := strings.TrimPrefix(prefix, "teams/")
		if i := strings.Index(rest, "/"); i > 0 {
			return rest[:i]
		}
	}
	return ""
}
