import time
from functools import cached_property, lru_cache
from typing import Type, Callable, Generator, Any, List, Union, Dict, Iterable, Optional, Tuple, Literal

from fastapi import Request, Depends, FastAPI, Query, HTTPException
from pydantic import BaseModel
from pydantic.fields import ModelField
from sqlalchemy import delete, Column, Table, insert
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine, AsyncEngine
from sqlalchemy.orm import InstrumentedAttribute, RelationshipProperty
from sqlmodel import SQLModel
from sqlmodel.main import SQLModelMetaclass
from starlette import status
from starlette.responses import HTMLResponse, JSONResponse, Response, RedirectResponse
from starlette.templating import Jinja2Templates

from fastapi_amis_admin.amis.components import Page, TableCRUD, Action, ActionType, Dialog, Form, FormItem, Picker, \
    Remark, Service, Iframe, PageSchema, TableColumn, ColumnOperation, App, Grid, Avatar
from fastapi_amis_admin.amis.constants import LevelEnum, DisplayModeEnum
from fastapi_amis_admin.amis.types import BaseAmisApiOut, BaseAmisModel, AmisAPI, SchemaNode
from fastapi_amis_admin.amis_admin.parser import AmisParser
from fastapi_amis_admin.fastapi_crud._base import RouterMixin
from fastapi_amis_admin.fastapi_crud._sqlmodel import SQLModelCrud, SQLModelSelector
from fastapi_amis_admin.fastapi_crud.parser import SQLModelFieldParser, SQLModelField, SQLModelListField
from fastapi_amis_admin.fastapi_crud.schema import CrudEnum, BaseApiOut
from fastapi_amis_admin.fastapi_crud.utils import parser_item_id, \
    schema_create_by_schema, parser_str_set_list
from fastapi_amis_admin.utils.db import SqlalchemyAsyncClient
from .settings import Settings


class LinkModelForm:
    link_model: Table  # 中间模型,与model 存在外键关联
    display_admin_cls: Type["ModelAdmin"]  # 关联模型admin
    session_factory: Callable[..., Generator[AsyncSession, Any, None]] = None  # session生成器

    def __init__(self,
                 pk_admin: "BaseModelAdmin",
                 display_admin_cls: Type["ModelAdmin"],
                 link_model: Union[SQLModel, Table],
                 link_col: Column,
                 item_col: Column,
                 session_factory: Callable[..., Generator[AsyncSession, Any, None]] = None):
        self.link_model = link_model
        self.pk_admin = pk_admin
        self.display_admin_cls = display_admin_cls or self.display_admin_cls
        if self.display_admin_cls not in self.pk_admin.app._admins_dict:
            raise f'{self.display_admin_cls} display_admin_cls is not register'
        self.display_admin: ModelAdmin = self.pk_admin.app.create_admin_instance(self.display_admin_cls)  # type:ignore
        assert isinstance(self.display_admin, ModelAdmin)
        self.session_factory = session_factory or self.pk_admin.session_factory
        self.link_col = link_col
        self.item_col = item_col
        assert self.item_col is not None, 'item_col is None'
        assert self.link_col is not None, 'link_col is None'
        self.path = '/' + self.display_admin_cls.model.__name__.lower()

    @classmethod
    def bind_model_admin(cls, pk_admin: "BaseModelAdmin", insfield: InstrumentedAttribute) -> Optional[
        "LinkModelForm"]:
        if not isinstance(insfield.prop, RelationshipProperty):
            return None
        table = insfield.prop.secondary
        if table is None:
            return None
        admin = None
        link_key = None
        item_key = None
        for key in table.foreign_keys:
            if key.column.table != pk_admin.model.__table__:  # 获取关联第三方表
                admin = pk_admin.app.get_model_admin(key.column.table.name)
                link_key = key  # auth_group.id
            else:
                item_key = key  # auth_user.id
        if admin and link_key and item_key:
            admin.link_models.update(
                {pk_admin.model.__tablename__: (table, link_key.parent, item_key.parent)})  # 注册内联模型
            return LinkModelForm(pk_admin=pk_admin,
                                 display_admin_cls=admin.__class__, link_model=table, link_col=link_key.parent,
                                 item_col=item_key.parent)
        return None

    @property
    def route_delete(self):
        async def route(
                request: Request,
                item_id: List[str] = Depends(parser_item_id),
                link_id: Union[int, str] = Query(..., min_length=1, title='link_id', example='1,2,3',
                                                 description='link model Primary key or list of link model primary keys'),
                db: AsyncSession = Depends(self.session_factory)
        ):
            if not await self.pk_admin.has_update_permission(request, item_id, None):
                return self.pk_admin.error_no_router_permission(request)
            link_id = parser_str_set_list(link_id)
            stmt = delete(self.link_model).where(self.link_col.in_(link_id)).where(self.item_col.in_(item_id))
            result = await db.execute(stmt)
            if result.rowcount:  # type: ignore
                await db.commit()
            return BaseApiOut(data=result.rowcount)  # type: ignore

        return route

    @property
    def route_create(self):
        async def route(
                request: Request,
                item_id: List[str] = Depends(parser_item_id),
                link_id: Union[int, str] = Query(..., min_length=1, title='link_id', example='1,2,3',
                                                 description='link model Primary key or list of link model primary keys'),
                db: AsyncSession = Depends(self.session_factory)
        ):
            if not await self.pk_admin.has_update_permission(request, item_id, None):
                return self.pk_admin.error_no_router_permission(request)
            link_id = parser_str_set_list(link_id)
            values = []
            for item in item_id:
                for link in link_id:
                    values.append({self.link_col.key: link, self.item_col.key: item})
            stmt = insert(self.link_model).values(values).prefix_with('OR IGNORE')
            result = await db.execute(stmt)
            if result.rowcount:  # type: ignore
                await db.commit()
            return BaseApiOut(data=result.rowcount)  # type: ignore

        return route

    async def get_form_item(self, request: Request):
        url = self.pk_admin.app.router_path + self.display_admin.router.url_path_for('page')
        picker = Picker(name=self.display_admin_cls.model.__tablename__, label=self.display_admin_cls.page_schema.label,
                        labelField='name',
                        valueField='id', multiple=True,
                        required=False, modalMode='dialog', size='full',
                        pickerSchema={'&': '${body}'},
                        source={'method': 'post', 'data': '${body.api.data}',
                                'url': '${body.api.url}&link_model=' + self.pk_admin.model.__tablename__ + '&link_item_id=${api.qsOptions.id}'})
        adaptor = None
        if await self.pk_admin.has_update_permission(request, None, None):  # type:ignore
            button_create = ActionType.Ajax(actionType='ajax', label='添加关联', level=LevelEnum.danger,
                                            confirmText='确定要添加关联?',
                                            api=f"post:{self.pk_admin.app.router_path}{self.pk_admin.router.prefix}{self.path}" + '/${REPLACE(query.link_item_id, "!", "")}?link_id=${IF(ids, ids, id)}')  # query.link_item_id
            adaptor = 'if(("undefined"==typeof body_bulkActions_2)||!body_bulkActions_2){action=' + button_create.amisJson() + ';payload.data.body.bulkActions.push(action);payload.data.body.itemActions.push(action);body_bulkActions_2=payload.data.body.bulkActions;body_itemActions_2=payload.data.body.itemActions;}else{payload.data.body.bulkActions=body_bulkActions_2;payload.data.body.itemActions=body_itemActions_2;}return payload;'
            button_create_dialog = ActionType.Dialog(type='button', icon='fa fa-plus pull-left', actionType='dialog',
                                                     label='添加关联',
                                                     level=LevelEnum.danger,
                                                     dialog=Dialog(title='添加关联', size='full', body=Service(
                                                         schemaApi=AmisAPI(method='get', url=url,
                                                                           responseData={'&': '${body}',
                                                                                         'api.url': '${body.api.url}&link_model=' + self.pk_admin.model.__tablename__ + '&link_item_id=!${api.qsOptions.id}'},
                                                                           qsOptions={'id': '$id'}, adaptor=adaptor)
                                                     ))
                                                     )

            button_delete = ActionType.Ajax(actionType='ajax', label='移除关联', level=LevelEnum.danger,
                                            confirmText='确定要移除关联?',
                                            api=f"delete:{self.pk_admin.app.router_path}{self.pk_admin.router.prefix}{self.path}" + '/${query.link_item_id}?link_id=${IF(ids, ids, id)}')  # ${IF(ids, ids, id)} # ${ids|raw}${id}
            adaptor = 'if(("undefined"==typeof body_bulkActions_1)||!body_bulkActions_1){action=' + button_delete.amisJson() + ';payload.data.body.headerToolbar.push(' + button_create_dialog.amisJson() + ');payload.data.body.bulkActions.push(action);payload.data.body.itemActions.push(action);body_headerToolbar_1=payload.data.body.headerToolbar;body_bulkActions_1=payload.data.body.bulkActions;body_itemActions_1=payload.data.body.itemActions;}else{payload.data.body.headerToolbar=body_headerToolbar_1;payload.data.body.bulkActions=body_bulkActions_1;payload.data.body.itemActions=body_itemActions_1;}return payload;'
        return Service(
            schemaApi=AmisAPI(method='get', url=url, cache=20000, responseData=dict(controls=[picker]),
                              qsOptions={'id': '$id'},
                              adaptor=adaptor)
        )

    def register_router(self):
        self.pk_admin.router.add_api_route(
            self.path + '/{item_id}',
            self.route_delete,
            methods=["DELETE"],
            response_model=BaseApiOut[int],
            name=self.link_model.name + '_Delete'
        )
        self.pk_admin.router.add_api_route(
            self.path + '/{item_id}',
            self.route_create,
            methods=["POST"],
            response_model=BaseApiOut[int],
            name=self.link_model.name + '_Create'
        )
        return self


class BaseModelAdmin(SQLModelCrud):
    list_display: List[Union[SQLModelListField, TableColumn]] = []  # 需要显示的字段
    list_filter: List[Union[SQLModelListField, FormItem]] = []  # 需要查询的字段
    list_per_page: int = 15  # 每页数据量
    link_model_fields: List[InstrumentedAttribute] = []  # 内联字段
    link_model_forms: List[LinkModelForm] = []
    bulk_edit_fields: List[Union[SQLModelListField, FormItem]] = []  # 批量编辑字段
    search_fields: List[SQLModelField] = []  # 模糊搜索字段

    def __init__(self, app: "AdminApp"):
        assert self.model, 'model is None'
        assert app, 'app is None'
        self.app = app
        self.session_factory = self.session_factory or self.app.db.session_factory
        self.parser = SQLModelFieldParser(default_model=self.model)
        self.fields = self.fields or self.parser.filter_insfield(self.list_display)
        super().__init__(self.model, self.session_factory)

    @cached_property
    def router_path(self) -> str:
        return self.app.router_path + self.router.prefix

    async def get_list_display(self, request: Request) -> List[Union[SQLModelListField, TableColumn]]:
        return self.list_display or list(self.schema_list.__fields__.values())

    async def get_list_filter(self, request: Request) -> List[Union[SQLModelListField, FormItem]]:
        return self.list_filter or list(self.schema_filter.__fields__.values())

    async def get_list_column(self, request: Request, modelfield: ModelField) -> TableColumn:
        return AmisParser(modelfield).as_table_column()

    async def get_form_item_on_foreign_key(self, request: Request, modelfield: ModelField) -> Union[
        Service, SchemaNode]:
        column = self.parser.get_column(modelfield.alias)
        foreign_keys = list(column.foreign_keys) or None
        if column is None or foreign_keys is None:
            return None
        admin = self.app.get_model_admin(foreign_keys[0].column.table.name)
        if not admin:
            return None
        url = self.app.router_path + admin.router.url_path_for('page')
        label = modelfield.field_info.title or modelfield.name
        remark = Remark(content=modelfield.field_info.description) if modelfield.field_info.description else None
        picker = Picker(name=modelfield.alias, label=label, labelField='name', valueField='id',
                        required=modelfield.required, modalMode='dialog'
                        , size='full', labelRemark=remark, pickerSchema='${body}', source='${body.api}')
        return Service(
            schemaApi=AmisAPI(method='get', url=url, cache=20000, responseData=dict(controls=[picker])))

    async def get_form_item(self, request: Request, modelfield: ModelField, action: CrudEnum) -> Union[
        FormItem, SchemaNode]:
        is_filter = action == CrudEnum.list
        return await self.get_form_item_on_foreign_key(request, modelfield) or AmisParser(modelfield).as_form_item(
            is_filter=is_filter)

    def get_link_model_forms(self) -> List[LinkModelForm]:
        return list(
            filter(None, [LinkModelForm.bind_model_admin(self, insfield) for insfield in self.link_model_fields]))

    async def get_list_columns(self, request: Request) -> List[TableColumn]:
        columns = []
        for field in await self.get_list_display(request):
            if isinstance(field, BaseAmisModel):
                columns.append(field)
            elif isinstance(field, SQLModelMetaclass):
                ins_list = self.parser.get_sqlmodel_insfield(field)  # type:ignore
                modelfield_list = [self.parser.get_modelfield(ins) for ins in ins_list]
                columns.extend([await self.get_list_column(request, modelfield) for modelfield in modelfield_list])
            else:
                columns.append(await self.get_list_column(request, self.parser.get_modelfield(field)))
        for link_form in self.link_model_forms:
            form = await link_form.get_form_item(request)
            if form:
                columns.append(ColumnOperation(
                    width=160, label=link_form.display_admin_cls.page_schema.label, breakpoint='*',
                    buttons=[form]
                ))
        return columns

    async def get_list_filter_form(self, request: Request) -> Form:
        body = await self._conv_modelfields_to_formitems(request, await self.get_list_filter(request),
                                                         CrudEnum.list)
        form = Form(type='', title='数据筛选', name=CrudEnum.list, body=body, mode=DisplayModeEnum.inline,
                    actions=[
                        Action(actionType='clear-and-submit', label='清空', level=LevelEnum.default),
                        Action(actionType='reset-and-submit', label='重置', level=LevelEnum.default),
                        Action(actionType='submit', label='搜索', level=LevelEnum.primary)], trimValues=True)
        return form

    async def get_list_filter_api(self, request: Request) -> AmisAPI:
        data = {'&': '$$'}
        for field in self.search_fields:
            alias = self.parser.get_alias(field)
            if alias:
                data.update({alias: '[~]$' + alias})
        api = AmisAPI(method='POST', url=f'{self.router_path}/list?' + 'page=${page}&perPage=${perPage}',
                      data=data)
        return api

    async def get_list_table(self, request: Request) -> TableCRUD:
        headerToolbar = ["filter-toggler", "reload", "bulkActions", {"type": "columns-toggler", "align": "right"},
                         {"type": "drag-toggler", "align": "right"}, {"type": "pagination", "align": "right"},
                         {"type": "tpl", "tpl": "当前有 ${total} 条数据.", "className": "v-middle", "align": "right"}]
        headerToolbar.extend(await self.get_actions_on_header_toolbar(request))
        table = TableCRUD(
            api=await self.get_list_filter_api(request),
            autoFillHeight=True,
            headerToolbar=headerToolbar,
            filterTogglable=True,
            filterDefaultVisible=False,
            filter=await self.get_list_filter_form(request),
            syncLocation=False,
            keepItemSelectionOnPageChange=True,
            perPage=self.list_per_page,
            itemActions=await self.get_actions_on_item(request),
            bulkActions=await self.get_actions_on_bulk(request),
            footerToolbar=["statistics", "switch-per-page", "pagination", "load-more", "export-csv"],
            columns=await self.get_list_columns(request),
        )
        if self.link_model_forms:
            table.footable = True
        return table

    async def get_create_form(self, request: Request, bulk: bool = False) -> Form:
        api = f'post:{self.router_path}/item'
        fields = [field for field in self.schema_create.__fields__.values() if field.name != self.pk]
        form = Form(api=api, name=CrudEnum.create,
                    body=await self._conv_modelfields_to_formitems(request, fields, CrudEnum.create), submitText=None)
        return form

    async def get_update_form(self, request: Request, bulk: bool = False) -> Form:
        if bulk == False:
            api = f'put:{self.router_path}/item/$id'
            fields = self.schema_update.__fields__.values()
        else:
            api = f'put:{self.router_path}/item/' + '${ids|raw}'
            fields = self.bulk_edit_fields
        form = Form(api=api, name=CrudEnum.update,
                    body=await self._conv_modelfields_to_formitems(request, fields, CrudEnum.update), submitText=None,
                    trimValues=True)
        return form

    async def get_actions_on_header_toolbar(self, request: Request) -> List[Action]:
        actions = []
        if await self.has_create_permission(request, None):
            actions.append(
                ActionType.Dialog(type='button', icon='fa fa-plus pull-left', actionType='dialog', label='新增',
                                  level=LevelEnum.primary,
                                  dialog=Dialog(title='新增', body=await self.get_create_form(request, bulk=False))))
        return actions

    async def get_actions_on_item(self, request: Request) -> List[Action]:
        buttons = []
        if await self.has_update_permission(request, None, None):  # type:ignore
            buttons.append(ActionType.Dialog(icon='fa fa-pencil', actionType='dialog', tooltip='编辑',
                                             dialog=Dialog(title='编辑', size='lg',
                                                           body=await self.get_update_form(request, bulk=False))))
        if await self.has_delete_permission(request, None):  # type:ignore
            buttons.append(ActionType.Ajax(icon='fa fa-times text-danger', actionType='ajax', tooltip='删除',
                                           confirmText='您确认要删除?',
                                           api=f"delete:{self.router_path}/item/$id"))
        return buttons

    async def get_actions_on_bulk(self, request: Request) -> List[Action]:
        bulkActions = []
        if await self.has_delete_permission(request, None):  # type:ignore
            bulkActions.append(
                ActionType.Ajax(actionType='ajax', label='批量删除',
                                confirmText='确定要批量删除?',
                                api=f"delete:{self.router_path}/item/" + '${ids|raw}')
            )
        # 开启批量编辑
        if self.bulk_edit_fields and await self.has_update_permission(request, None, None):  # type:ignore
            bulkActions.append(
                ActionType.Dialog(actionType='dialog', label='批量修改',
                                  dialog=Dialog(title='批量修改',
                                                body=await self.get_update_form(request, bulk=True))
                                  ))
        return bulkActions

    async def _conv_modelfields_to_formitems(self, request: Request,
                                             fields: Iterable[Union[SQLModelListField, ModelField, FormItem]],
                                             action: CrudEnum = None) -> List[FormItem]:
        items = []
        for field in fields:
            if isinstance(field, FormItem):
                items.append(field)
            else:
                field = self.parser.get_modelfield(field)
                if field:
                    item = await self.get_form_item(request, field, action)
                    if item:
                        items.append(item)
        return items


class BaseAdmin:
    def __init__(self, app: "AdminApp"):
        self.app = app
        assert self.app, 'app is None'


class PageSchemaAdmin(BaseAdmin):
    group_schema: Union[PageSchema, str] = PageSchema()
    page_schema: Union[PageSchema, str] = PageSchema()

    def __init__(self, app: "AdminApp"):
        super().__init__(app)
        self.page_schema = self.get_page_schema()
        self.group_schema = self.get_group_schema()

    async def has_page_permission(self, request: Request) -> bool:
        return True

    def error_no_page_permission(self, request: Request):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail='No page permissions')

    def get_page_schema(self) -> Optional[PageSchema]:
        if self.page_schema:
            if isinstance(self.page_schema, str):
                self.page_schema = PageSchema(label=self.page_schema)
            elif isinstance(self.page_schema, PageSchema):
                self.page_schema = self.page_schema.copy(deep=True)
                self.page_schema.label = self.page_schema.label or self.__class__.__name__
            else:
                raise TypeError()
        return self.page_schema

    def get_group_schema(self) -> Optional[PageSchema]:
        if self.group_schema:
            if isinstance(self.group_schema, str):
                self.group_schema = PageSchema(label=self.group_schema)
            elif isinstance(self.group_schema, PageSchema):
                self.group_schema = self.group_schema.copy(deep=True)
                self.group_schema.label = self.group_schema.label or 'default'
            else:
                raise TypeError()
        return self.group_schema


class LinkAdmin(PageSchemaAdmin):
    link: str = ''

    def get_page_schema(self) -> Optional[PageSchema]:
        super().get_page_schema()
        if self.page_schema:
            self.page_schema.link = self.page_schema.link or self.link
        return self.page_schema


class IframeAdmin(PageSchemaAdmin):
    iframe: Iframe = None
    src: str = ''

    def get_page_schema(self) -> Optional[PageSchema]:
        super().get_page_schema()
        if self.page_schema:
            self.page_schema.url = f'/{self.__class__.__name__}'
            iframe = self.iframe or Iframe(src=self.src)
            self.page_schema.schema_ = Page(body=iframe)
        return self.page_schema


class RouterAdmin(BaseAdmin, RouterMixin):
    def __init__(self, app: "AdminApp"):
        BaseAdmin.__init__(self, app)
        RouterMixin.__init__(self)

    def register_router(self):
        raise NotImplementedError()

    @cached_property
    def router_path(self) -> str:
        if self.router is self.app.router:
            return self.app.router_path
        return self.app.router_path + self.router.prefix


class PageAdmin(PageSchemaAdmin, RouterAdmin):
    '''amis普通页面'''
    page: Page = None
    page_path: Optional[str] = None
    page_parser_mode: Literal["json", "html", "jinja2"] = 'json'
    template_name: str = ''
    page_route_kwargs: Dict[str, Any] = {}
    router_prefix = ''

    def __init__(self, app: "AdminApp"):
        RouterAdmin.__init__(self, app)
        if self.page_path is None:
            self.page_path = f'/{self.__class__.__module__}/{self.__class__.__name__}.json'
        PageSchemaAdmin.__init__(self, app)

    async def page_permission_depend(self, request: Request) -> bool:
        return await self.has_page_permission(request) or self.error_no_page_permission(request)

    async def get_page(self, request: Request) -> Page:
        return self.page or Page()

    def get_page_schema(self) -> Optional[PageSchema]:
        super().get_page_schema()
        if self.page_schema:
            self.page_schema.url = f'{self.router.prefix}{self.page_path}'
            self.page_schema.schemaApi = f'get:{self.router_path}{self.page_path}'
            if self.page_parser_mode == 'html':
                self.page_schema.schema_ = Page(body=Iframe(src=self.page_schema.schemaApi))
        return self.page_schema

    def page_parser(self, request: Request, page: Page) -> Response:
        mode = request.query_params.get('_parser') or self.page_parser_mode
        result = None
        if mode == 'json':
            result = BaseAmisApiOut(data=page.amisDict())
            result = JSONResponse(result.dict())
        elif mode == 'html':
            result = page.amis_html(self.template_name)
            result = HTMLResponse(result)
        return result

    def register_router(self):
        kwargs = {**self.page_route_kwargs}
        if self.page_parser_mode == 'json':
            kwargs.update(dict(response_model=BaseAmisApiOut))
        else:
            kwargs.update(dict(response_class=HTMLResponse, include_in_schema=False))
        self.router.add_api_route(
            self.page_path,
            self.route_page,
            methods=["GET"],
            name='page', **kwargs,
            dependencies=[Depends(self.page_permission_depend)]
        )

    @property
    def route_page(self) -> Callable:
        async def route(request: Request, page: Page = Depends(self.get_page)):
            return self.page_parser(request, page)

        return route


class FormAdmin(PageAdmin):
    form: Form = None
    form_init: bool = None
    schema: Type[BaseModel] = None
    schema_init_out: Type[BaseModel] = None
    schema_submit_out: Type[BaseModel] = None

    def __init__(self, site: "AdminApp"):
        super().__init__(site)
        assert self.schema, 'schema is None'

    async def get_page(self, request: Request) -> Page:
        page = await super(FormAdmin, self).get_page(request)
        page.body = await self.get_form(request)
        return page

    async def get_form(self, request: Request) -> Form:
        form = self.form or Form()
        form.api = f"post:{self.router_path}{self.page_path}"
        form.title = ''  # self.page_schema.label
        form.body = [AmisParser(modelfield).as_form_item() for modelfield in
                     self.schema.__fields__.values()]
        return form

    async def handle(self, request: Request,
                     data: "self.schema", **kwargs) -> BaseApiOut["self.schema_submit_out"]:  # type:ignore
        return BaseApiOut(data=data)

    @property
    def route_submit(self):
        async def route(request: Request, data: self.schema):  # type:ignore
            return await self.handle(request, data)

        return route

    def register_router(self):
        super().register_router()
        # submit
        self.router.add_api_route(self.page_path, self.route_submit, methods=["POST"],
                                  response_model=BaseApiOut[self.schema_submit_out],
                                  dependencies=[Depends(self.page_permission_depend)])
        # init
        if self.form_init:
            self.schema_init_out = self.schema_init_out or schema_create_by_schema(self.schema, 'InitOut',
                                                                                   set_none=True)
            self.router.add_api_route(self.page_path + '/init', self.route_init, methods=["GET"],
                                      response_model=BaseApiOut[self.schema_init_out],
                                      dependencies=[Depends(self.page_permission_depend)])

    async def get_init_data(self, request: Request, **kwargs) \
            -> BaseApiOut["self.schema_init_out"]:  # type:ignore
        return BaseApiOut(data=None)

    @property
    def route_init(self):
        async def route(request: Request):
            return await self.get_init_data(request)

        return route


class ModelFormAdmin(FormAdmin, SQLModelSelector):
    '''todo Read and update a model resource '''

    def __init__(self, site: "AdminApp"):
        FormAdmin.__init__(self, site)
        SQLModelSelector.__init__(self)


class TemplateAdmin(PageAdmin):
    '''jinja2模板渲染页'''
    page: Dict[str, Any] = {}
    page_parser_mode = 'html'
    templates: Jinja2Templates = Jinja2Templates(directory='templates')

    def __init__(self, app: "AdminApp"):
        self.page_path = self.page_path or '/' + self.template_name
        super().__init__(app)

    def page_parser(self, request: Request, page: Dict[str, Any]):
        page['request'] = request
        return self.templates.TemplateResponse(self.template_name, page)

    async def get_page(self, request: Request) -> Dict[str, Any]:
        return {}


class ModelAdmin(BaseModelAdmin, PageAdmin):
    page_path: str = '/amis.json'
    bind_model: bool = True
    group_schema = None

    def __init__(self, app: "AdminApp"):
        BaseModelAdmin.__init__(self, app)
        PageAdmin.__init__(self, app)

    def register_router(self):
        self.link_model_forms: List[LinkModelForm] = self.get_link_model_forms()
        for form in self.link_model_forms:
            form.register_router()
        self.register_crud()
        super(ModelAdmin, self).register_router()

    async def get_page(self, request: Request) -> Page:
        page = await super(ModelAdmin, self).get_page(request)
        page.body = await self.get_list_table(request)
        return page


class AdminApp(PageAdmin):
    group_schema: Union[PageSchema, str] = None
    engine: AsyncEngine = None
    page_path = '/amis.json'
    page_parser_mode = 'json'

    def __init__(self, app: "AdminApp"):
        super().__init__(app)
        self.engine = self.engine or self.app.site.engine
        assert self.engine, 'engine is None'
        self.db = SqlalchemyAsyncClient(self.engine)
        self._pages_dict: Dict[str, Tuple[PageSchema, List[Union[PageSchema, BaseAdmin]]]] = {}
        self._admins_dict: Dict[Type[BaseAdmin], Optional[BaseAdmin]] = {}

    def create_admin_instance(self, admin_cls: Type[BaseAdmin]):
        admin = self._admins_dict.get(admin_cls)
        if admin is not None or not issubclass(admin_cls, BaseAdmin):
            return admin
        admin = admin_cls(self)
        self._admins_dict[admin_cls] = admin
        if isinstance(admin, PageSchemaAdmin):
            group_label = admin.group_schema and admin.group_schema.label
            if admin.page_schema:
                if not self._pages_dict.get(group_label):
                    self._pages_dict[group_label] = (admin.group_schema, [])
                self._pages_dict[group_label][1].append(admin)
        return admin

    def create_admin_instance_all(self):
        [self.create_admin_instance(admin_cls) for admin_cls in self._admins_dict.keys()]

    def _register_admin_router_all(self):
        for admin in self._admins_dict.values():
            if isinstance(admin, RouterAdmin):  # 注册路由
                admin.register_router()
                self.router.include_router(admin.router)

    def on_register_router_pre(self):
        pass

    def route_index(self):
        return RedirectResponse(url=self.router_path + self.page_path + '?_parser=html')

    def register_router(self):
        '''注册Admin站点路由'''
        t = time.time()
        super(AdminApp, self).register_router()
        self.router.add_api_route('/', self.route_index, name='index', include_in_schema=False)
        self.on_register_router_pre()
        self.create_admin_instance_all()
        self._register_admin_router_all()
        print('register_router time', time.time() - t)

    @cached_property
    def site(self) -> "BaseAdminSite":
        if isinstance(self.app, BaseAdminSite):
            return self.app
        return self.app.site

    @lru_cache
    def get_model_admin(self, table_name: str) -> Optional[ModelAdmin]:
        for admin_cls, admin in self._admins_dict.items():
            if issubclass(admin_cls,
                          ModelAdmin) and admin_cls.bind_model and admin_cls.model.__tablename__ == table_name:
                return admin
            elif isinstance(admin, AdminApp):
                return admin.get_model_admin(table_name)
        return None

    async def get_page_schema_children(self, request: Request) -> List[PageSchema]:
        children = []
        for group_label, (group_schema, admins_list) in self._pages_dict.items():
            lst = []
            for admin in admins_list:
                if admin and isinstance(admin, PageSchemaAdmin):
                    if await admin.has_page_permission(request):
                        if isinstance(admin, AdminApp):
                            sub_children = await admin.get_page_schema_children(request)
                            if sub_children:
                                page_schema = admin.page_schema.copy(deep=True)
                                page_schema.children = sub_children
                                lst.append(page_schema)
                        else:
                            lst.append(admin.page_schema)
                else:
                    lst.append(admin)
            if lst:
                if group_label:
                    lst.sort(key=lambda p: p.sort or 0, reverse=True)
                    group_schema = group_schema.copy(deep=True)
                    group_schema.children = lst
                    children.append(group_schema)
                else:  # ModelAdmin
                    children.extend(lst)
        if children:
            children.sort(key=lambda p: p.sort or 0, reverse=True)
        return children

    def register_admin(self, admin_cls: Type[BaseAdmin]):
        self._admins_dict.update({admin_cls: None})
        return admin_cls

    def unregister_admin(self, admin_cls: Type[BaseAdmin]):
        self._admins_dict.pop(admin_cls)

    def setup_admin(self, *admin_cls: Type[BaseAdmin]):
        [self.register_admin(cls) for cls in admin_cls if cls]
        return self

    async def get_page(self, request: Request) -> App:
        app = App(api=self.router_path + self.page_path)
        app.brandName = 'AmisAdmin'
        # app.header = Tpl(className='w-full',tpl='<div class="flex justify-between"><div>顶部区域左侧</div><div>顶部区域右侧</div></div>')
        app.header = Grid(align='left', columns=[Grid.Column(md=11), Grid.Column(md=1, body=[
            Avatar(src='https://suda.cdn.bcebos.com/images/amis/ai-fake-face.jpg',
                   size=30)])])
        app.footer = '<div class="p-2 text-center bg-light">FastAPI-Amis-Admin</div>'
        # app.asideBefore = '<div class="p-2 text-center">菜单前面区域</div>'
        # app.asideAfter = '<div class="p-2 text-center">菜单后面区域</div>'
        _parser = request.query_params.get('_parser') or self.page_parser_mode
        if _parser == 'json':
            app.pages = []
            children = await self.get_page_schema_children(request)
            if not children:
                return app
            if self is self.site:
                app.pages.extend(children)
            else:
                page_schema = self.page_schema.copy(deep=True)
                page_schema.children = children
                app.pages.append(page_schema)
        return app


class BaseAdminSite(AdminApp):

    def __init__(self, settings: Settings, root_path='/admin', fastapi: FastAPI = None,
                 engine: AsyncEngine = None):
        self.fastapi = fastapi or FastAPI(debug=settings.debug, reload=settings.debug)
        self.router = self.fastapi.router

        self.root_path = root_path
        self.settings = settings
        self.engine = engine or create_async_engine(settings.database_url_async, echo=settings.debug, future=True,
                                                    pool_recycle=1200)
        super().__init__(self)

    @cached_property
    def router_path(self) -> str:
        return self.root_path + self.router.prefix

    def mount_app(self, fastapi: FastAPI, name=None):
        self.register_router()  # 注册路由
        fastapi.mount(self.router_path, self.fastapi, name=name)  # 挂载admin