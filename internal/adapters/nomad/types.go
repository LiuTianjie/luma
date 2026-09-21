// Package nomad maps deployment policy to Nomad's wire format. It performs no I/O.
package nomad

type JobEnvelope struct{ Job Job }
type Job struct {
	ID, Name, Type string
	Datacenters    []string
	Constraints    []Constraint `json:",omitempty"`
	Update         Update
	TaskGroups     []Group
	Meta           map[string]string
}
type Constraint struct {
	LTarget string
	RTarget string `json:",omitempty"`
	Operand string
}
type Update struct {
	AutoRevert                                        bool
	MaxParallel                                       int
	MinHealthyTime, HealthyDeadline, ProgressDeadline int64
	HealthCheck                                       string
	Canary                                            int  `json:",omitempty"`
	AutoPromote                                       bool `json:",omitempty"`
}
type Group struct {
	Name                string
	Count               int
	MaxClientDisconnect int64
	Networks            []Network          `json:",omitempty"`
	Services            []DiscoveryService `json:",omitempty"`
	Tasks               []Task
}
type Network struct {
	Mode          string
	ReservedPorts []Port `json:",omitempty"`
	DynamicPorts  []Port `json:",omitempty"`
}
type Port struct {
	Label       string
	Value       int `json:",omitempty"`
	To          int
	HostNetwork string `json:",omitempty"`
}
type DiscoveryService struct {
	Name, PortLabel, Provider string
	Tags                      []string `json:",omitempty"`
	Address                   string   `json:",omitempty"`
	AddressMode               string   `json:",omitempty"`
	Checks                    []Check  `json:",omitempty"`
}
type Check struct {
	Type, Name, PortLabel string
	Path                  string `json:",omitempty"`
	Interval, Timeout     int64
}
type Task struct {
	Name, Driver string
	Config       DockerConfig
	Resources    Resources
	Env          map[string]string `json:",omitempty"`
	KillTimeout  *int64            `json:",omitempty"`
}
type DockerConfig struct {
	Image       string    `json:"image"`
	NetworkMode string    `json:"network_mode,omitempty"`
	Ports       []string  `json:"ports,omitempty"`
	Args        *[]string `json:"args,omitempty"`
	Mount       []Mount   `json:"mount,omitempty"`
	ExtraHosts  []string  `json:"extra_hosts,omitempty"`
}
type Mount struct {
	Type     string `json:"type"`
	Target   string `json:"target"`
	Source   string `json:"source"`
	Readonly bool   `json:"readonly"`
}
type Resources struct {
	CPU, MemoryMB int
	MemoryMaxMB   int `json:",omitempty"`
}
