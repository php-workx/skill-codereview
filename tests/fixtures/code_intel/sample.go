package main

import (
	"fmt"
	"strings"
	"os"
)

// PublicFunction is exported.
func PublicFunction(name string, count int) string {
	return fmt.Sprintf("%s: %d", name, count)
}

// privateHelper is unexported.
func privateHelper(s string) string {
	return strings.TrimSpace(s)
}

// ComplexHandler has high cyclomatic complexity.
func ComplexHandler(value int, mode string, flag bool) string {
	if value > 100 {
		if mode == "strict" {
			if flag {
				return "high-strict-flag"
			} else {
				return "high-strict-noflag"
			}
		} else if mode == "relaxed" {
			return "high-relaxed"
		} else {
			return "high-other"
		}
	} else if value > 50 {
		for i := 0; i < value; i++ {
			if i%2 == 0 && flag {
				return fmt.Sprintf("mid-even-%d", i)
			} else if i%3 == 0 || mode == "special" {
				continue
			}
		}
		return "mid-default"
	}
	return "low"
}

func main() {
	result := PublicFunction("test", 42)
	fmt.Println(result)
	privateHelper("  hello  ")
	os.Exit(0)
}
