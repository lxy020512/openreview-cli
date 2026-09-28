-- Migration 015: persist a graph node's declared parent.
-- graph_nodes dropped GraphNode.parent_id, so a graph read back from SQLite
-- could never report an orphan (a clause naming a parent the document lacks).
ALTER TABLE graph_nodes ADD COLUMN parent_id TEXT;
