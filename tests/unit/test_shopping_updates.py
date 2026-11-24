from app.agents.product_requirements import protect_arguments, update_requirements


def initial():
    return update_requirements("推荐300元以内黑色42码的鞋")


def test_append_and_replace_colors_preserve_other_requirements():
    added = update_requirements("白色也可以", initial())
    assert added.active
    assert added.constraints == {"max_price_cents": 30000, "size": "42", "colors": ["白色", "黑色"]}
    replaced = update_requirements("换成白色", added)
    assert replaced.constraints == {"max_price_cents": 30000, "size": "42", "color": "白色"}
    assert replaced.task_query == "鞋"


def test_change_budget_and_remove_color():
    changed = update_requirements("预算改成350元", initial())
    assert changed.clarification is None
    assert changed.constraints["max_price_cents"] == 35000
    removed = update_requirements("颜色不限", changed)
    assert "color" not in removed.constraints
    assert removed.constraints["size"] == "42"
    args = protect_arguments({"query": "黑色鞋", "color": "黑色"}, removed)
    assert "color" not in args
    assert args["query"] == "鞋"


def test_switch_product_starts_fresh_and_can_explicitly_supply_budget():
    changed = update_requirements("再推荐一个耳机", initial())
    assert changed.active
    assert changed.task_query == "耳机"
    assert changed.constraints == {}
    budget = update_requirements("推荐耳机，也控制在300元以内", initial())
    assert budget.constraints == {"max_price_cents": 30000}


def test_unrelated_and_aftersales_turns_do_not_route_to_shopping():
    for query in ("我的退款多久到账", "鞋开胶了能退吗", "谢谢", "耳机保修多久"):
        assert not update_requirements(query, initial()).active


def test_ambiguous_update_keeps_previous_conditions_until_clarified():
    previous = initial()
    changed = update_requirements("预算300元左右", previous)
    assert changed.clarification
    assert changed.constraints == previous.constraints
