pub fn implemented_fn(x: i32) -> i32 {
    let y = x + 1;
    let z = y * 2;
    if z > 10 {
        return z - 1;
    }
    z
}

pub fn stub_fn() {
    todo!()
}

pub fn another_stub() {
    unimplemented!()
}
