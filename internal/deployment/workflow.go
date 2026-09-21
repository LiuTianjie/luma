package deployment

import (
	"context"
	"github.com/LiuTianjie/luma/internal/workflow"
)

type Recipe = workflow.Recipe
type Selector = workflow.Selector
type Workflow struct {
	Recipe           Recipe   `json:"recipe"`
	Selector         Selector `json:"selector"`
	ExpectedRevision int      `json:"expectedRevision"`
	Note             *string  `json:"note,omitempty"`
}
type WorkflowCheck struct {
	Name        string
	Status      string
	Revision    int
	Differences string
}
type WorkflowStore interface {
	Check(context.Context, Workflow) (WorkflowCheck, error)
	Record(context.Context, Workflow, []byte) error
}
