package server

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"

	v1beta1 "github.com/agentscope-ai/AgentTeams/agentteams-controller/api/v1beta1"
	"github.com/agentscope-ai/AgentTeams/agentteams-controller/internal/auth"
	"github.com/agentscope-ai/AgentTeams/agentteams-controller/internal/httputil"
	"k8s.io/apimachinery/pkg/types"
	"sigs.k8s.io/controller-runtime/pkg/client"
)

// annotationLoopPausedBy records which source last paused the loop.
// Written by the pause endpoint, cleared by the resume endpoint.
const annotationLoopPausedBy = "loops.agentteams.io/paused-by"

// Valid values for the pause body's "source" field.
const (
	pauseSourceUser    = "user"
	pauseSourceProject = "project"
)

// LoopHandler serves the Loop control plane endpoints (INT-7b):
// POST /api/v1/loops/{id}/pause and POST /api/v1/loops/{id}/resume.
// It patches spec only — the status subresource is written exclusively
// by the loop reconciler.
type LoopHandler struct {
	Client    client.Client
	Namespace string
}

// NewLoopHandler builds a LoopHandler.
func NewLoopHandler(c client.Client, namespace string) *LoopHandler {
	return &LoopHandler{Client: c, Namespace: namespace}
}

// PauseLoop handles POST /api/v1/loops/{id}/pause.
//
// Body (optional): {"source": "user" | "project"}, default "user".
// Sets spec.paused=true and records the source in the
// loops.agentteams.io/paused-by annotation (last-writer-wins).
//
// Permission (INT-7b ruling): any authenticated role except worker —
// admin/manager unrestricted, teamLeader/human restricted to their own
// team (enforced here, see checkLoopAccess).
func (h *LoopHandler) PauseLoop(w http.ResponseWriter, r *http.Request) {
	id := r.PathValue("id")
	ctx := r.Context()

	loop := &v1beta1.Loop{}
	if err := h.Client.Get(ctx, h.loopKey(id), loop); err != nil {
		httputil.WriteError(w, http.StatusNotFound, fmt.Sprintf("loop %q not found", id))
		return
	}

	var body struct {
		Source string `json:"source"`
	}
	if err := json.NewDecoder(r.Body).Decode(&body); err != nil && !errors.Is(err, io.EOF) {
		httputil.WriteError(w, http.StatusBadRequest, `invalid request body: expected a JSON object with an optional "source" field`)
		return
	}
	source := body.Source
	if source == "" {
		source = pauseSourceUser
	}
	if source != pauseSourceUser && source != pauseSourceProject {
		httputil.WriteError(w, http.StatusBadRequest,
			fmt.Sprintf("invalid pause source %q: must be %q or %q", source, pauseSourceUser, pauseSourceProject))
		return
	}

	if err := h.checkLoopAccess(ctx, loop, false); err != nil {
		httputil.WriteError(w, http.StatusForbidden, err.Error())
		return
	}

	patch := client.MergeFrom(loop.DeepCopy())
	loop.Spec.Paused = true
	if loop.Annotations == nil {
		loop.Annotations = map[string]string{}
	}
	loop.Annotations[annotationLoopPausedBy] = source
	if err := h.Client.Patch(ctx, loop, patch); err != nil {
		httputil.WriteError(w, http.StatusInternalServerError, fmt.Sprintf("failed to pause loop: %v", err))
		return
	}
	httputil.WriteJSON(w, http.StatusOK, map[string]any{
		"id": loop.Name, "paused": true, "source": source,
	})
}

// ResumeLoop handles POST /api/v1/loops/{id}/resume.
//
// Permission (INT-7b ruling): admin + L2 human only — manager, team
// leader, and worker are denied deliberately (a paused loop is a human
// decision; agents do not release it themselves).
//
// Semantics:
//   - loop not paused             -> 200 no-op (idempotent)
//   - paused by "project" or ""   -> 200, clears spec.paused and the
//     paused-by annotation
//   - paused by "user" (or any
//     other value)                -> 409 with the current pause source
//     in the body — a user-scope pause must be lifted by the user's own
//     interaction, never by a project-driven flow
func (h *LoopHandler) ResumeLoop(w http.ResponseWriter, r *http.Request) {
	id := r.PathValue("id")
	ctx := r.Context()

	loop := &v1beta1.Loop{}
	if err := h.Client.Get(ctx, h.loopKey(id), loop); err != nil {
		httputil.WriteError(w, http.StatusNotFound, fmt.Sprintf("loop %q not found", id))
		return
	}

	if err := h.checkLoopAccess(ctx, loop, true); err != nil {
		httputil.WriteError(w, http.StatusForbidden, err.Error())
		return
	}

	if !loop.Spec.Paused {
		httputil.WriteJSON(w, http.StatusOK, map[string]any{
			"id": loop.Name, "paused": false, "resumed": false,
		})
		return
	}

	source := loop.Annotations[annotationLoopPausedBy]
	if source != "" && source != pauseSourceProject {
		httputil.WriteJSON(w, http.StatusConflict, map[string]any{
			"message":        fmt.Sprintf("loop is paused by source %q; only that source may resume it", source),
			"current_source": source,
		})
		return
	}

	patch := client.MergeFrom(loop.DeepCopy())
	loop.Spec.Paused = false
	delete(loop.Annotations, annotationLoopPausedBy)
	if err := h.Client.Patch(ctx, loop, patch); err != nil {
		httputil.WriteError(w, http.StatusInternalServerError, fmt.Sprintf("failed to resume loop: %v", err))
		return
	}
	httputil.WriteJSON(w, http.StatusOK, map[string]any{
		"id": loop.Name, "paused": false, "resumed": true,
	})
}

func (h *LoopHandler) loopKey(id string) types.NamespacedName {
	return types.NamespacedName{Namespace: h.Namespace, Name: id}
}

// checkLoopAccess re-checks the caller against the loop. It is defense
// in depth on top of the RequireAuthz middleware, and the REAL
// team-scope boundary: the middleware's resolveResourceTeam only
// resolves workers, so a loop's ResourceTeam arrives empty and
// requireSameTeam short-circuits — the same pattern as the project
// endpoints.
func (h *LoopHandler) checkLoopAccess(ctx context.Context, loop *v1beta1.Loop, resume bool) error {
	caller := auth.CallerFromContext(ctx)
	if caller == nil {
		// No identity in context (e.g. direct handler invocation
		// without authentication): the middleware layer is responsible;
		// nothing to check here.
		return nil
	}
	switch caller.Role {
	case auth.RoleAdmin:
		return nil
	case auth.RoleManager:
		if resume {
			return fmt.Errorf("authorization denied: loop resume is restricted to admin and L2 human")
		}
		return nil
	case auth.RoleTeamLeader:
		if resume {
			return fmt.Errorf("authorization denied: loop resume is restricted to admin and L2 human")
		}
		return loopTeamAccess(caller, loop.Spec.TeamName)
	case auth.RoleHuman:
		return loopTeamAccess(caller, loop.Spec.TeamName)
	case auth.RoleWorker:
		return fmt.Errorf("authorization denied: worker may not control loops")
	default:
		return fmt.Errorf("authorization denied: unknown role %q", caller.Role)
	}
}

// loopTeamAccess mirrors auth.requireSameTeam semantics against the
// loop's own team: a caller with no team at all is denied; a loop
// without a team (standalone) is reachable by any team-scoped caller;
// otherwise the caller must belong to the loop's team.
func loopTeamAccess(caller *auth.CallerIdentity, teamName string) error {
	if caller.Team == "" && len(caller.Teams) == 0 {
		return fmt.Errorf("authorization denied: %s %q has no team", caller.Role, caller.Username)
	}
	if teamName == "" {
		return nil
	}
	if !caller.TeamMatches(teamName) {
		return fmt.Errorf("authorization denied: %s %q cannot access loop in team %q",
			caller.Role, caller.Username, teamName)
	}
	return nil
}
