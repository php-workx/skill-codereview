package main

import "fmt"

func goodHandler() error {
	result, err := doSomething()
	if err != nil {
		return err
	}
	fmt.Println(result)
	return nil
}

func badHandler() {
	result, err := doSomething()
	_ = err
	fmt.Println(result)
}

func emptyHandler() {
	defer func() {
		if r := recover(); r != nil {
		}
	}()
}

// TODO: implement proper error handling
