package main

import "os"

var filepathSep = os.PathSeparator

func readDirSafe(dir string) ([]os.DirEntry, error) { return os.ReadDir(dir) }

func relative(root, full string) string {
	if len(full) > len(root)+1 {
		return full[len(root)+1:]
	}
	return full
}

func writeFile(path string, data []byte) error {
	return os.WriteFile(path, data, 0o644)
}